"""Uploads developed JPEGs to Google Photos, one at a time, never twice, never deleting anything."""
from __future__ import annotations

import json
import logging
import threading
import time

from .db import db
from .gphotos import GoogleError
from .processor import valid_jpeg

log = logging.getLogger("photomanager.upload")
MAX_ATTEMPTS = 3
STOP_TEXT = {
    "cancelled": "Stopped",
    "disabled": "Stopped because Google Photos upload was switched off",
    "auth": "Stopped — connect Google Photos again",
    "rate": "Paused because Google's upload limit was reached — it will carry on later",
    "nas": "Stopped because the NAS isn't available",
}


class UploadError(Exception):
    pass


class Stop(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class Uploader:
    def __init__(self, cfg, storage, google, on_done=lambda: None):
        self.cfg, self.storage, self.google, self.on_done = cfg, storage, google, on_done
        self._lock = threading.Lock()
        self._cancel = False
        self._only = None
        self.state = self._blank()
        self.state["finished"] = None
        try:
            with db(cfg) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='last_upload'").fetchone()
            if row:
                self.state.update(json.loads(row[0]), running=False)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _blank():
        return {"running": False, "trigger": None, "started": None, "finished": None, "total": 0,
                "done": 0, "failed": 0, "current": None, "stop_reason": None, "stop_text": None,
                "error": None}

    @property
    def enabled(self) -> bool:
        return bool(self.storage.settings.get("google_upload"))

    # --- queue ---------------------------------------------------------------------------------
    def pending(self) -> list[tuple]:
        """Developed JPEGs not yet in Google Photos. JPEGs that were already on the NAS before
        we got there (not made by us) are never uploaded."""
        only = self._only
        with db(self.cfg) as conn:
            rows = conn.execute(
                "SELECT d.rel, d.out_rel, d.out_size FROM developed d "
                "LEFT JOIN uploaded u ON u.out_rel=d.out_rel "
                "WHERE d.status='done' AND d.error IS NULL AND d.out_rel IS NOT NULL "
                "AND (u.out_rel IS NULL OR (u.status='failed' AND u.attempts < ?)) "
                "ORDER BY d.at, d.rel", (MAX_ATTEMPTS,)).fetchall()
        return [r for r in rows if r[0] in only] if only else rows

    def counts(self) -> dict:
        with db(self.cfg) as conn:
            up = conn.execute("SELECT COUNT(*) FROM uploaded WHERE status='done'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM uploaded WHERE status='failed'").fetchone()[0]
            gave_up = conn.execute("SELECT COUNT(*) FROM uploaded WHERE status='failed' AND attempts>=?",
                                   (MAX_ATTEMPTS,)).fetchone()[0]
        return {"pending": len(self.pending()), "uploaded": up, "failed": failed, "gave_up": gave_up}

    def failures(self, limit=50) -> list[dict]:
        with db(self.cfg) as conn:
            rows = conn.execute("SELECT out_rel, error, attempts FROM uploaded WHERE status='failed' "
                                "ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [{"rel": r, "error": e, "attempts": a, "will_retry": a < MAX_ATTEMPTS} for r, e, a in rows]

    def retry_failed(self) -> int:
        with db(self.cfg) as conn:
            return conn.execute("UPDATE uploaded SET attempts=0 WHERE status='failed'").rowcount

    def summary(self) -> dict:
        return {"enabled": self.enabled, "state": dict(self.state), "counts": self.counts(),
                "failed": self.failures(), **self.google.status()}

    # --- control -------------------------------------------------------------------------------
    def start(self, trigger: str, only=None) -> dict:
        if trigger != "manual" and not self.enabled:      # "Upload now" works with the switch off
            raise UploadError("Automatic upload to Google Photos is turned off")
        if not self.google.connected:
            raise UploadError("Connect Google Photos first")
        with self._lock:
            if self.state["running"]:
                raise UploadError("Uploading is already running")
            self._cancel = False
            self._only = set(only) if only else None
            self.state = {**self._blank(), "running": True, "trigger": trigger, "started": time.time()}
        threading.Thread(target=self._run, args=(trigger,), daemon=True).start()
        return dict(self.state)

    def cancel(self):
        self._cancel = True

    def scheduled_tick(self, develop_running: bool) -> bool:
        """Every 30 s: pick up JPEGs that are waiting (e.g. after an earlier pause)."""
        if develop_running or not self.enabled or self.state["running"] or not self.google.connected:
            return False
        if time.time() - (self.state.get("finished") or 0) < 1800:
            return False
        if not self.pending():
            return False
        try:
            self.start("schedule")
        except UploadError:
            return False
        return True

    def _stop_reason(self, trigger):
        if self._cancel:
            return "cancelled"
        if trigger != "manual" and not self.enabled:
            return "disabled"
        return None

    def _run(self, trigger):
        st = self.state
        try:
            rows = self.pending()
            st["total"] = len(rows)
            for rel, out_rel, out_size in rows:
                reason = self._stop_reason(trigger)
                if reason:
                    raise Stop(reason)
                st["current"] = out_rel
                self._one(rel, out_rel, out_size)
        except Stop as stop:
            st.update(stop_reason=stop.reason, stop_text=STOP_TEXT.get(stop.reason, stop.reason),
                      error=stop.detail)
        except Exception as exc:  # noqa: BLE001
            log.exception("upload run failed")
            st["error"] = str(exc)
        st.update(finished=time.time(), current=None)
        try:
            with db(self.cfg) as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_upload', ?)",
                             (json.dumps({**st, "running": False}),))
        except Exception:  # noqa: BLE001
            log.exception("could not save last upload")
        st["running"] = False
        self._only = None
        log.info("Google Photos upload (%s): %d uploaded, %d failed%s", trigger, st["done"], st["failed"],
                 f", {st['stop_text']}" if st["stop_reason"] else "")
        self.on_done()

    def _record(self, out_rel, raw_rel, status, size=None, media_id=None, error=None):
        with db(self.cfg) as conn:
            conn.execute(
                "INSERT INTO uploaded (out_rel,raw_rel,size,media_id,status,attempts,error,at) "
                "VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(out_rel) DO UPDATE SET status=excluded.status, "
                "size=excluded.size, media_id=excluded.media_id, error=excluded.error, "
                "attempts=uploaded.attempts+1, at=excluded.at",
                (out_rel, raw_rel, size, media_id, status, error, time.time()))

    def _one(self, rel, out_rel, out_size):
        st = self.state
        try:
            path = self.storage.abs_path(out_rel)
            if not path.is_file():
                raise GoogleError("The JPEG is no longer on the NAS")
            if out_size is not None and path.stat().st_size != out_size:
                raise GoogleError("The JPEG changed since it was developed, so it was not uploaded")
            if not valid_jpeg(path):
                raise GoogleError("The JPEG looks damaged, so it was not uploaded")
            media_id = self.google.upload(path, path.name)
        except GoogleError as exc:
            if exc.kind == "auth":
                raise Stop("auth", str(exc)) from None
            if exc.kind == "rate":
                raise Stop("rate", str(exc)) from None
            self._record(out_rel, rel, "failed", error=str(exc))
            st["failed"] += 1
            log.warning("Could not upload %s: %s", out_rel, exc)
            return
        except OSError as exc:
            self._record(out_rel, rel, "failed", error=f"Can't read the JPEG: {exc.strerror or exc}")
            st["failed"] += 1
            return
        self._record(out_rel, rel, "done", size=out_size, media_id=media_id)
        st["done"] += 1
