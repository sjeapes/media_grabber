"""Develops CR2 files to JPEGs with darktable-cli — only after the user turns it on.

How a photo is developed (all of it safe for the photo folder):
  1. The RAW file is copied to a private temp folder on the add-on's own disk, so darktable
     can never write anything (not even a .xmp sidecar) next to your photos.
  2. darktable-cli runs on the copy with its default settings (automatic look), headless,
     at the lowest CPU priority.
  3. The JPEG is checked, copied to the NAS under a hidden temporary name and then renamed,
     so a half-written file is never left where it looks finished.
  4. An existing file is never overwritten, and the RAW file is never touched.

JPEGs are named <name>_developed.jpg — never the same as the camera's own IMG_0001.JPG.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .db import db
from .storage import disk

log = logging.getLogger("photomanager.develop")
MAX_ATTEMPTS = 3          # a photo that fails is retried on later runs, this many times in all

STOP_TEXT = {
    "cancelled": "Stopped by you",
    "disabled": "Stopped because developing was turned off",
    "window": "Paused at the end of the overnight window — it continues next night",
    "low_space": "Stopped because the NAS is running low on space",
    "nas": "Stopped because the NAS stopped responding",
    "scan": "Stopped because the scan failed",
}


class DevelopError(Exception):
    """A problem with one photo (or with starting a run); the message is shown to the user."""


class Stop(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def valid_jpeg(path: Path) -> bool:
    """A complete JPEG starts with FFD8 and ends with FFD9."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2)
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64))
            tail = fh.read().rstrip(b"\0")
    except OSError:
        return False
    return size > 0 and head == b"\xff\xd8" and tail.endswith(b"\xff\xd9")


class Processor:
    def __init__(self, cfg, storage, scanner, on_done=None):
        self.cfg = cfg
        self.storage = storage
        self.scanner = scanner
        self.on_done = on_done or (lambda: None)
        self.clock = datetime.now            # replaced in tests
        self._lock = threading.Lock()
        self._cancel = False
        self._dt_info = None
        self.state = self._blank_state()
        with db(cfg) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='last_develop'").fetchone()
        if row:
            try:
                self.state.update(json.loads(row[0]), running=False)
            except ValueError:
                pass
        shutil.rmtree(cfg.tmp_dir / "work", ignore_errors=True)   # leftovers from a crash

    @staticmethod
    def _blank_state() -> dict:
        return {"running": False, "trigger": None, "phase": None, "started": None,
                "finished": None, "total": 0, "done": 0, "failed": 0, "existing": 0,
                "current": None, "stop_reason": None, "stop_text": None, "error": None}

    # --- darktable ------------------------------------------------------------------
    def darktable(self) -> dict:
        path = shutil.which(self.cfg.darktable)
        if not path:
            return {"available": False, "version": None,
                    "error": "darktable isn't installed in this add-on"}
        if self._dt_info is None:
            version = None
            try:
                out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                     timeout=60).stdout.strip().splitlines()
                version = out[0] if out else None
            except (OSError, subprocess.SubprocessError):
                pass
            self._dt_info = {"available": True, "version": version, "error": None}
        return dict(self._dt_info)

    # --- overnight window ------------------------------------------------------------
    def window(self) -> tuple[int, int]:
        return int(self.cfg.opt("develop_start_hour")), int(self.cfg.opt("develop_end_hour"))

    def window_id(self, now: datetime | None = None) -> str | None:
        """The date the current overnight window started on, or None outside the window."""
        now = now or self.clock()
        start, end = self.window()
        today = now.strftime("%Y-%m-%d")
        if start == end:                       # same hour twice = any time of day
            return today
        if start < end:
            return today if start <= now.hour < end else None
        if now.hour >= start:                  # window runs past midnight
            return today
        if now.hour < end:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return None

    def in_window(self) -> bool:
        return self.window_id() is not None

    # --- queue -----------------------------------------------------------------------
    def pending(self) -> tuple[list[tuple], int]:
        """(rows to develop, identical copies skipped). Identical copies are developed once."""
        s = self.storage.settings
        scope = s.get("develop_scope") or ""
        with db(self.cfg) as conn:
            rows = conn.execute(
                "SELECT p.rel, p.size, p.sha256 FROM photos p LEFT JOIN developed d ON d.rel=p.rel "
                "WHERE (d.rel IS NULL OR (d.status='failed' AND d.attempts < ?)) "
                + ("AND p.has_jpeg=0 " if s.get("skip_with_jpeg") else "")
                + "ORDER BY p.taken DESC, p.rel", (MAX_ATTEMPTS,)).fetchall()
            if scope:
                rows = [r for r in rows if r[0] == scope or r[0].startswith(scope + "/")]
            done_shas = {r[0] for r in conn.execute(
                "SELECT p.sha256 FROM developed d JOIN photos p ON p.rel=d.rel "
                "WHERE d.status='done' AND p.sha256 IS NOT NULL "
                "UNION SELECT sha256 FROM developed WHERE status='done' AND sha256 IS NOT NULL")}
        keep, seen, skipped = [], set(), 0
        for rel, size, sha in rows:
            if sha:
                if sha in done_shas or sha in seen:
                    skipped += 1
                    continue
                seen.add(sha)
            keep.append((rel, size, sha))
        return keep, skipped

    def counts(self) -> dict:
        pending, dup_skipped = self.pending()
        with db(self.cfg) as conn:
            done = conn.execute("SELECT COUNT(*) FROM developed WHERE status='done'").fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM developed WHERE status='failed'").fetchone()[0]
            gave_up = conn.execute("SELECT COUNT(*) FROM developed WHERE status='failed' "
                                   "AND attempts >= ?", (MAX_ATTEMPTS,)).fetchone()[0]
            photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            with_jpeg = conn.execute("SELECT COUNT(*) FROM photos p LEFT JOIN developed d "
                                     "ON d.rel=p.rel WHERE p.has_jpeg=1 AND d.rel IS NULL"
                                     ).fetchone()[0]
        return {"with_camera_jpeg": with_jpeg, "photos": photos, "pending": len(pending), "developed": done, "failed": failed,
                "gave_up": gave_up, "duplicates_skipped": dup_skipped}

    def failures(self, limit: int = 50) -> list[dict]:
        with db(self.cfg) as conn:
            rows = conn.execute("SELECT rel, error, attempts, at FROM developed "
                                "WHERE status='failed' ORDER BY at DESC LIMIT ?", (limit,))
            return [{"rel": r, "error": e, "attempts": a, "at": t,
                     "will_retry": a < MAX_ATTEMPTS} for r, e, a, t in rows]

    def retry_failed(self) -> int:
        with db(self.cfg) as conn:
            return conn.execute("UPDATE developed SET attempts=0 WHERE status='failed'").rowcount

    def example(self) -> dict | None:
        """Where the JPEG for one real photo would go, so the user can see the effect of the
        'where to save' choice."""
        folder = self.storage.settings.get("photo_folder")
        if not folder:
            return None
        with db(self.cfg) as conn:
            row = conn.execute("SELECT rel FROM photos ORDER BY taken DESC, rel LIMIT 1").fetchone()
        rel = row[0] if row else "2024/IMG_1234.CR2"
        return {"raw": f"{folder}/{rel}", "jpeg": self.storage.developed_rel(rel)}

    # --- control ----------------------------------------------------------------------
    def start(self, trigger: str, window: str | None = None) -> dict:
        """Start a run in the background. Raises DevelopError if it can't start."""
        if trigger == "schedule" and not self.storage.settings["processing_enabled"]:
            raise DevelopError("Automatic developing is turned off")     # manual runs don't need it
        dt = self.darktable()
        if not dt["available"]:
            raise DevelopError(dt["error"])
        self.storage.resolve()
        if self.storage.photo_dir() is None:
            raise DevelopError("Choose your photo folder on the Storage tab first")
        with self._lock:
            if self.state["running"]:
                raise DevelopError("Developing is already running")
            self._cancel = False
            self.state = {**self._blank_state(), "running": True, "trigger": trigger,
                          "phase": "scanning", "started": time.time()}
        threading.Thread(target=self._run, args=(trigger, window), daemon=True).start()
        return dict(self.state)

    def cancel(self):
        self._cancel = True

    def scheduled_tick(self) -> bool:
        """Called every half minute: start tonight's run when it is due. Returns True if started."""
        if not self.storage.settings["processing_enabled"] or self.state["running"]:
            return False
        wid = self.window_id()
        if wid is None:
            return False
        with db(self.cfg) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='develop_done_window'").fetchone()
        if row and row[0] == wid:
            return False
        try:
            self.start("schedule", window=wid)
        except DevelopError as exc:
            log.info("Scheduled develop run not started: %s", exc)
            return False
        return True

    # --- the run ------------------------------------------------------------------------
    def _should_stop(self, trigger: str) -> str | None:
        if self._cancel:
            return "cancelled"
        if trigger == "schedule" and not self.storage.settings["processing_enabled"]:
            return "disabled"
        if trigger == "schedule" and not self.in_window():
            return "window"
        return None

    def _run(self, trigger: str, window: str | None):
        st = self.state
        try:
            self._scan_first()
            rows, _skipped = self.pending()
            st["total"] = len(rows)
            st["phase"] = "developing"
            base = self.cfg.base_path
            min_free = float(self.cfg.opt("develop_min_free_gb"))
            for rel, size, sha in rows:
                reason = self._should_stop(trigger)
                if reason:
                    raise Stop(reason)
                try:
                    if disk(base)["free_gb"] < min_free:
                        raise Stop("low_space")
                except Stop:
                    raise
                except Exception:  # noqa: BLE001 - NAS hung or vanished
                    raise Stop("nas") from None
                st["current"] = rel
                self._develop_one(rel, size, sha)
        except Stop as stop:
            st.update(stop_reason=stop.reason, stop_text=STOP_TEXT.get(stop.reason, stop.reason),
                      error=stop.detail)
        except Exception as exc:  # noqa: BLE001
            log.exception("develop run failed")
            st["error"] = str(exc)
        st.update(finished=time.time(), phase=None, current=None)
        # Record tonight's window as done BEFORE flagging the run as finished, so the
        # scheduler can never see "not running" and start the same night's run again.
        if trigger == "schedule" and window and not st["stop_reason"] and not st["error"]:
            self._set_meta("develop_done_window", window)
        self._set_meta("last_develop", json.dumps({**st, "running": False}))
        shutil.rmtree(self.cfg.tmp_dir / "work", ignore_errors=True)
        st["running"] = False
        log.info("Develop run (%s): %d developed, %d failed%s", trigger, st["done"], st["failed"],
                 f", {st['stop_text']}" if st["stop_reason"] else "")
        self.on_done()

    def _set_meta(self, key: str, value: str):
        try:
            with db(self.cfg) as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        except Exception:  # noqa: BLE001
            log.exception("could not save %s", key)

    def _scan_first(self):
        """A quick incremental scan so photos added since the last one are included."""
        self.scanner.start(self.storage.photo_dir())   # False = one is already running: wait for it
        while self.scanner.state["running"]:
            if self._cancel:
                self.scanner.cancel()
                raise Stop("cancelled")
            time.sleep(0.2)
        if self.scanner.state.get("error"):
            raise Stop("scan", f"The scan failed: {self.scanner.state['error']}")

    def _develop_one(self, rel: str, size: int, sha: str | None):
        st = self.state
        try:
            out_rel, out_size, existing = self._develop(rel, size)
        except Stop:
            raise
        except DevelopError as exc:
            self._record(rel, "failed", sha, size, error=str(exc))
            st["failed"] += 1
            log.warning("Could not develop %s: %s", rel, exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._record(rel, "failed", sha, size, error=f"{type(exc).__name__}: {exc}"[:300])
            st["failed"] += 1
            log.exception("Could not develop %s", rel)
            return
        self._record(rel, "done", sha, size, out_rel=out_rel, out_size=out_size,
                     error="A file with that name already existed and was left untouched"
                     if existing else None)
        st["done"] += 1
        st["existing"] += int(existing)

    def _record(self, rel, status, sha, size, out_rel=None, out_size=None, error=None):
        with db(self.cfg) as conn:
            conn.execute(
                "INSERT INTO developed (rel,status,sha256,raw_size,out_rel,out_size,attempts,at,error) "
                "VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(rel) DO UPDATE SET status=excluded.status, "
                "sha256=excluded.sha256, raw_size=excluded.raw_size, out_rel=excluded.out_rel, "
                "out_size=excluded.out_size, attempts=developed.attempts+1, at=excluded.at, "
                "error=excluded.error",
                (rel, status, sha, size, out_rel, out_size, time.time(), error))

    def _command(self, local_in: Path, local_out: Path) -> list[str]:
        tmp = self.cfg.tmp_dir
        cmd = [self.cfg.darktable, str(local_in), str(local_out), "--core",
               "--library", ":memory:", "--configdir", str(tmp / "darktable-config"),
               "--cachedir", str(tmp / "darktable-cache"), "--disable-opencl",
               "--conf", f"plugins/imageio/format/jpeg/quality={int(self.cfg.opt('jpeg_quality'))}",
               "--conf", "write_sidecar_files=never"]
        nice = shutil.which("nice")
        return [nice, "-n", "19", *cmd] if nice else cmd

    def _run_darktable(self, cmd: list[str]) -> tuple[int, str]:
        env = {**os.environ, "HOME": str(self.cfg.tmp_dir / "home")}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=env)
        t0 = time.monotonic()
        while True:
            try:
                out, _ = proc.communicate(timeout=0.5)
                return proc.returncode, out or ""
            except subprocess.TimeoutExpired:
                if self._cancel:
                    proc.kill()
                    proc.communicate()
                    raise Stop("cancelled") from None
                if time.monotonic() - t0 > self.cfg.dt_timeout:
                    proc.kill()
                    proc.communicate()
                    raise DevelopError(
                        f"darktable took longer than {max(1, self.cfg.dt_timeout // 60)} minutes") from None

    def _develop(self, rel: str, size: int) -> tuple[str, int, bool]:
        """Develop one RAW file. Returns (jpeg path relative to the NAS, size, already_existed)."""
        photo_dir = self.storage.photo_dir()
        if photo_dir is None:
            raise Stop("nas")
        src = photo_dir / rel
        out_rel = self.storage.developed_rel(rel)
        out_abs = self.storage.abs_path(out_rel)
        if out_abs.exists():                    # never overwrite anything
            return out_rel, out_abs.stat().st_size, True
        try:
            if src.stat().st_size != size:
                raise DevelopError("The file changed since the last scan — scan again")
        except OSError as exc:
            raise DevelopError(f"Can't read the RAW file: {exc.strerror or exc}") from None

        work = self.cfg.tmp_dir / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for d in ("home", "darktable-config", "darktable-cache"):
            (self.cfg.tmp_dir / d).mkdir(parents=True, exist_ok=True)
        local_in = work / ("input" + src.suffix.lower())
        local_out = work / "output.jpg"
        try:
            try:
                shutil.copyfile(src, local_in)
            except OSError as exc:
                raise DevelopError(f"Can't copy the RAW file: {exc.strerror or exc}") from None
            if local_in.stat().st_size != size:
                raise DevelopError("The RAW file wasn't copied completely")
            rc, output = self._run_darktable(self._command(local_in, local_out))
            if rc != 0 or not local_out.exists():
                tail = " ".join(output.strip().splitlines()[-3:])[-300:]
                raise DevelopError(f"darktable couldn't develop this file"
                                   f"{f' (exit {rc})' if rc else ''}{': ' + tail if tail else ''}")
            if not valid_jpeg(local_out):
                raise DevelopError("darktable produced a damaged JPEG")
            out_size = local_out.stat().st_size
            partial = out_abs.parent / f".{out_abs.name}.partial"
            try:
                out_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_out, partial)
                if partial.stat().st_size != out_size:
                    raise DevelopError("The JPEG wasn't copied to the NAS completely")
                if out_abs.exists():            # appeared meanwhile: keep it, drop ours
                    partial.unlink()
                    return out_rel, out_abs.stat().st_size, True
                os.replace(partial, out_abs)
            except OSError as exc:
                try:
                    partial.unlink()
                except OSError:
                    pass
                raise DevelopError(f"Can't save the JPEG on the NAS: {exc.strerror or exc}") from None
            except DevelopError:
                try:
                    partial.unlink()
                except OSError:
                    pass
                raise
            return out_rel, out_size, False
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # --- summary for the UI and Home Assistant ------------------------------------------
    def summary(self) -> dict:
        start, end = self.window()
        s = self.storage.settings
        return {"enabled": s["processing_enabled"], "output_mode": s["output_mode"],
                "scope": s["develop_scope"], "skip_with_jpeg": s["skip_with_jpeg"],
                "window": {"start": start, "end": end, "now_inside": self.in_window()},
                "state": dict(self.state), "counts": self.counts(), "darktable": self.darktable(),
                "jpeg_quality": int(self.cfg.opt("jpeg_quality")), "example": self.example(),
                "failed": self.failures()}
