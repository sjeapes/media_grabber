"""Finding out which photos are not in Google Photos yet, using a Google Takeout export.

Google no longer lets apps list your library, but a Takeout export contains a small .json
"sidecar" for every photo (its file name and capture time). Photo Manager reads ONLY those
sidecars — from .zip files or already-extracted folders — never the photos in them, and
never writes anything into the Takeout folder.

A photo here counts as already in Google Photos when
  * Photo Manager uploaded it itself, or
  * Google has a photo with the same file name (ignoring extension and a "_developed" ending)
    taken within a day of it (the same name can recur, so the time has to agree), or
  * (a weaker "possibly" match) Google has a photo taken within 2 seconds of it.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import threading
import time
import zipfile
from pathlib import Path

from .db import db

log = logging.getLogger("photomanager.takeout")
MAX_JSON = 1024 * 1024
NAME_WINDOW = 90000          # seconds: a day plus slack for time-zone differences
TIME_WINDOW = 2
MAX_SELECT = 5000
_SUFFIX = re.compile(r"(_developed|-edited|-edit)$")
_COPY = re.compile(r"\s*\(\d+\)$")


def normalise(title: str) -> str:
    """'IMG_0001_developed.JPG', 'IMG_0001(1).jpg' and 'img_0001.cr2' all become 'img_0001'."""
    base = str(title).strip().lower()
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = _COPY.sub("", base)
    return _SUFFIX.sub("", base)


def parse_sidecar(raw: bytes) -> tuple[str, str, int] | None:
    """(stem, title, timestamp) from one Takeout .json file, or None if it isn't a photo sidecar."""
    try:
        d = json.loads(raw)
        title = d["title"]
        ts = int(d["photoTakenTime"]["timestamp"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(title, str) or not title or d.get("trashed") is True:
        return None
    return normalise(title), title, ts


def taken_ts(taken: str | None) -> int | None:
    try:
        return calendar.timegm(time.strptime(taken, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


class Takeout:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cancel = False
        self._index = None            # (by_stem, by_ts), rebuilt after each scan
        self.state = {"running": False, "phase": None, "sources": 0, "entries": 0, "current": None,
                      "errors": 0, "error_samples": [], "error": None, "cancelled": False,
                      "started": None, "finished": None}
        try:
            with db(cfg) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='takeout_last'").fetchone()
            if row:
                self.state.update(json.loads(row[0]), running=False)
        except Exception:  # noqa: BLE001
            pass

    # --- scanning the export -----------------------------------------------------------------------
    def start(self, root: Path | None) -> bool:
        if root is None or not root.is_dir():
            raise ValueError("Choose the folder with your Google Takeout export first")
        with self._lock:
            if self.state["running"]:
                return False
            self._cancel = False
            self.state.update(running=True, phase="looking", sources=0, entries=0, current=None,
                              errors=0, error_samples=[], error=None, cancelled=False,
                              started=time.time(), finished=None)
        threading.Thread(target=self._scan, args=(root,), daemon=True).start()
        return True

    def cancel(self):
        self._cancel = True

    def _note_error(self, where: str, why: str):
        st = self.state
        st["errors"] += 1
        if len(st["error_samples"]) < 5:
            st["error_samples"].append(f"{where}: {why}")

    def _scan(self, root: Path):
        st = self.state
        try:
            with db(self.cfg) as conn:
                known = {p: (s, m) for p, s, m in conn.execute(
                    "SELECT path, size, mtime FROM takeout_sources")}
            seen = set()
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames.sort()
                for name in sorted(filenames):
                    if self._cancel:
                        raise InterruptedError
                    low = name.lower()
                    if not (low.endswith(".zip") or low.endswith(".json")):
                        continue
                    path = Path(dirpath) / name
                    rel = path.relative_to(root).as_posix()
                    try:
                        s = path.stat()
                    except OSError as exc:
                        self._note_error(rel, exc.strerror or str(exc))
                        continue
                    seen.add(rel)
                    st["current"] = rel
                    if known.get(rel) == (s.st_size, s.st_mtime):
                        continue                                  # unchanged since the last scan
                    st["phase"] = "reading"
                    try:
                        rows = (list(self._read_zip(path)) if low.endswith(".zip")
                                else list(self._read_json_file(path)))
                    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                        self._note_error(rel, f"can't read ({exc})")
                        continue
                    with db(self.cfg) as conn:
                        conn.execute("DELETE FROM takeout WHERE source=?", (rel,))
                        conn.executemany("INSERT INTO takeout (source, stem, title, ts) VALUES (?,?,?,?)",
                                         [(rel, *r) for r in rows])
                        conn.execute("INSERT OR REPLACE INTO takeout_sources VALUES (?,?,?,?)",
                                     (rel, s.st_size, s.st_mtime, len(rows)))
            with db(self.cfg) as conn:                            # forget files that have been removed
                for rel in set(known) - seen:
                    conn.execute("DELETE FROM takeout WHERE source=?", (rel,))
                    conn.execute("DELETE FROM takeout_sources WHERE path=?", (rel,))
                st["sources"] = conn.execute("SELECT COUNT(*) FROM takeout_sources").fetchone()[0]
                st["entries"] = conn.execute("SELECT COUNT(*) FROM takeout").fetchone()[0]
        except InterruptedError:
            st["cancelled"] = True
        except Exception as exc:  # noqa: BLE001
            log.exception("takeout scan failed")
            st["error"] = str(exc)
        self._index = None
        st.update(running=False, phase=None, current=None, finished=time.time())
        try:
            with db(self.cfg) as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('takeout_last', ?)",
                             (json.dumps({**st, "running": False}),))
        except Exception:  # noqa: BLE001
            pass

    def _read_json_file(self, path: Path):
        if path.stat().st_size <= MAX_JSON:
            r = parse_sidecar(path.read_bytes())
            if r:
                yield r

    def _read_zip(self, path: Path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if self._cancel:
                    raise InterruptedError
                if not info.filename.lower().endswith(".json") or info.file_size > MAX_JSON:
                    continue
                r = parse_sidecar(z.read(info))
                if r:
                    yield r

    # --- the index and matching -------------------------------------------------------------------
    def counts(self) -> dict:
        with db(self.cfg) as conn:
            return {"sources": conn.execute("SELECT COUNT(*) FROM takeout_sources").fetchone()[0],
                    "entries": conn.execute("SELECT COUNT(*) FROM takeout").fetchone()[0]}

    def _load(self):
        if self._index is None:
            by_stem, by_ts = {}, {}
            with db(self.cfg) as conn:
                for stem, ts in conn.execute("SELECT stem, ts FROM takeout"):
                    by_stem.setdefault(stem, []).append(ts)
                    by_ts[ts] = by_ts.get(ts, 0) + 1
            self._index = (by_stem, by_ts)
        return self._index

    def classify(self, rel: str, taken: str | None, taken_src: str | None, uploaded_here: bool) -> str:
        """'uploaded' (certain), 'possibly' (same capture time only) or 'missing'."""
        if uploaded_here:
            return "uploaded"
        if taken_src != "exif":
            return "missing"
        t = taken_ts(taken)
        if t is None:
            return "missing"
        by_stem, by_ts = self._load()
        stem = normalise(rel.rsplit("/", 1)[-1])
        if any(abs(ts - t) <= NAME_WINDOW for ts in by_stem.get(stem, ())):
            return "uploaded"
        if any((t + d) in by_ts for d in range(-TIME_WINDOW, TIME_WINDOW + 1)):
            return "possibly"
        return "missing"

    def report(self, folder: str = "", include_possible: bool = False, offset: int = 0,
               limit: int = 60) -> dict:
        folder = (folder or "").strip("/")
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
        with db(self.cfg) as conn:
            rows = conn.execute(
                "SELECT p.rel, p.taken, p.taken_src, p.size, "
                "(d.out_rel IS NOT NULL), (u.out_rel IS NOT NULL) FROM photos p "
                "LEFT JOIN developed d ON d.rel=p.rel AND d.status='done' AND d.out_rel IS NOT NULL "
                "LEFT JOIN uploaded u ON u.out_rel=d.out_rel AND u.status='done' "
                "ORDER BY p.taken DESC, p.rel").fetchall()
        counts = {"in_scope": 0, "uploaded": 0, "possibly": 0, "missing": 0}
        folders: dict[str, int] = {}
        wanted = []
        for rel, taken, src, size, developed, up in rows:
            if folder and not rel.startswith(folder + "/"):
                continue
            kind = self.classify(rel, taken, src, bool(up))
            counts["in_scope"] += 1
            counts[kind] += 1
            if kind == "missing" or (kind == "possibly" and include_possible):
                d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                folders[d] = folders.get(d, 0) + 1
                wanted.append({"rel": rel, "taken": taken, "size": size, "developed": bool(developed),
                               "match": "possibly" if kind == "possibly" else "missing"})
        top = sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))[:300]
        return {"counts": counts, "total": len(wanted), "offset": offset,
                "items": wanted[offset:offset + limit],
                "folders": [{"path": p, "count": n} for p, n in top], "folders_total": len(folders)}

    def resolve(self, rels, folders, include_possible: bool) -> list[str]:
        """The photos a selection means: the ticked photos plus every missing photo that is directly
        inside a ticked folder ("" = directly in the photo folder). Only photos that are really
        missing are returned, so a stale page can't upload something that has since been matched."""
        want = {r for r in (rels or []) if isinstance(r, str)}
        dirs = {f.strip("/") for f in (folders or []) if isinstance(f, str)}
        if not want and not dirs:
            return []
        with db(self.cfg) as conn:
            rows = conn.execute(
                "SELECT p.rel, p.taken, p.taken_src, (u.out_rel IS NOT NULL) FROM photos p "
                "LEFT JOIN developed d ON d.rel=p.rel AND d.status='done' AND d.out_rel IS NOT NULL "
                "LEFT JOIN uploaded u ON u.out_rel=d.out_rel AND u.status='done' "
                "ORDER BY p.taken DESC, p.rel").fetchall()
        chosen = []
        for rel, taken, src, up in rows:
            folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if rel not in want and folder not in dirs:
                continue
            kind = self.classify(rel, taken, src, bool(up))
            if kind == "missing" or (kind == "possibly" and include_possible):
                chosen.append(rel)
        return chosen[:MAX_SELECT]
