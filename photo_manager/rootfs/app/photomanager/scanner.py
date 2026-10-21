"""Read-only scan of the photo folder: finds CR2 files, reads their EXIF, finds exact duplicates.

Nothing in the photo folder is ever modified — files are only opened for reading. Results
are kept in a SQLite database in the add-on's private data folder (/data/photos.db), so a
re-scan only re-reads files whose size or modification time changed.

Duplicate detection is exact: only files that share a byte size can be identical, so just
those get a full SHA-256 hash (the others are never read in full, which keeps scans of a
network share quick).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import exifread

from .db import db, init
from .storage import is_skipped

log = logging.getLogger("photomanager.scan")
GB = 1024 ** 3
HASH_CHUNK = 4 * 1024 * 1024
BATCH = 200

class Cancelled(Exception):
    pass


def parse_exif_time(value: str) -> str | None:
    """EXIF 'YYYY:MM:DD HH:MM:SS' → ISO text; None if it isn't a real date."""
    try:
        return datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S") \
            .strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def read_exif(path: str) -> dict:
    """Capture time and camera from a CR2 (a TIFF container). Never raises."""
    out = {"taken": None, "camera": None, "error": None}
    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except Exception as exc:  # noqa: BLE001 - a bad file must not stop the scan
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    when = tags.get("EXIF DateTimeOriginal") or tags.get("EXIF DateTimeDigitized") \
        or tags.get("Image DateTime")
    if when is not None:
        out["taken"] = parse_exif_time(when.values if hasattr(when, "values") else when)
    model = tags.get("Image Model")
    make = tags.get("Image Make")
    if model is not None:
        name = str(model).strip()
        if make is not None and str(make).strip() and not name.lower().startswith(
                str(make).strip().lower().split()[0]):
            name = f"{str(make).strip()} {name}"
        out["camera"] = name
    if out["taken"] is None:
        out["error"] = "No capture time in the file"
    return out


def sha256_file(path: str, cancelled=lambda: False) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            if cancelled():
                raise Cancelled
            h.update(chunk)
    return h.hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class Scanner:
    def __init__(self, cfg, on_done=None):
        self.cfg = cfg
        self.on_done = on_done or (lambda: None)
        self._lock = threading.Lock()
        self._cancel = False
        self.state = self._blank_state()
        init(cfg)
        with self._db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        if row:
            try:
                self.state.update(json.loads(row[0]), running=False)
            except ValueError:
                pass

    @staticmethod
    def _blank_state() -> dict:
        return {"running": False, "root": None, "phase": None, "started": None, "finished": None,
                "files": 0, "bytes": 0, "to_hash": 0, "hashed": 0, "errors": 0,
                "current": None, "cancelled": False, "error": None}

    def _db(self):
        return db(self.cfg)

    # --- control -----------------------------------------------------------------
    def start(self, root: Path) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self._cancel = False
            self.state = {**self._blank_state(), "running": True, "root": str(root),
                          "phase": "walking", "started": time.time()}
        threading.Thread(target=self._run, args=(Path(root),), daemon=True).start()
        return True

    def cancel(self):
        self._cancel = True

    def _check(self):
        if self._cancel:
            raise Cancelled

    # --- the scan ----------------------------------------------------------------------
    def _run(self, root: Path):
        try:
            with self._db() as conn:
                self._scan(conn, root)
            self.state["finished"] = time.time()
        except Cancelled:
            self.state.update(cancelled=True, finished=time.time())
        except Exception as exc:  # noqa: BLE001
            log.exception("scan failed")
            self.state.update(error=str(exc), finished=time.time())
        self.state.update(running=False, current=None, phase=None)
        try:
            with self._db() as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)",
                             (json.dumps(self.state),))
        except sqlite3.Error:
            log.exception("could not save scan state")
        log.info("Scan %s: %d CR2 files, %d read errors", "cancelled" if self.state["cancelled"]
                 else "failed" if self.state["error"] else "finished",
                 self.state["files"], self.state["errors"])
        self.on_done()

    def _scan(self, conn: sqlite3.Connection, root: Path):
        st = self.state
        row = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if row is None or row[0] != str(root):
            conn.execute("DELETE FROM photos")  # a different folder is a different library
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(root),))
        known = {r[0]: r[1:] for r in conn.execute("SELECT rel, size, mtime FROM photos")}
        seen: set[str] = set()
        batch: list[tuple] = []

        def flush():
            if batch:
                conn.executemany(
                    "INSERT INTO photos (rel,size,mtime,taken,taken_src,camera,has_jpeg,sha256,"
                    "exif_error) VALUES (?,?,?,?,?,?,?,NULL,?) ON CONFLICT(rel) DO UPDATE SET "
                    "size=excluded.size, mtime=excluded.mtime, taken=excluded.taken, "
                    "taken_src=excluded.taken_src, camera=excluded.camera, "
                    "has_jpeg=excluded.has_jpeg, sha256=NULL, exif_error=excluded.exif_error",
                    batch)
                conn.commit()
                batch.clear()

        stack = [root]
        while stack:
            self._check()
            folder = stack.pop()
            st["current"] = os.path.relpath(folder, root) if folder != root else ""
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except OSError:
                st["errors"] += 1
                continue
            jpeg_stems, cr2s = set(), []
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not is_skipped(e.name):
                            stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                        low = e.name.lower()
                        if low.endswith(".cr2"):
                            cr2s.append(e)
                        elif low.endswith((".jpg", ".jpeg")):
                            jpeg_stems.add(low.rsplit(".", 1)[0])
                except OSError:
                    st["errors"] += 1
            for e in cr2s:
                self._check()
                try:
                    info = e.stat(follow_symlinks=False)
                except OSError:
                    st["errors"] += 1
                    continue
                rel = Path(os.path.relpath(e.path, root)).as_posix()
                seen.add(rel)
                st["files"] += 1
                st["bytes"] += info.st_size
                has_jpeg = int(e.name.lower()[:-4] in jpeg_stems)
                if known.get(rel) == (info.st_size, info.st_mtime):
                    conn.execute("UPDATE photos SET has_jpeg=? WHERE rel=?", (has_jpeg, rel))
                    continue
                st["phase"] = "reading"
                meta = read_exif(e.path)
                if meta["error"] and meta["taken"] is None and meta["camera"] is None:
                    st["errors"] += 1
                taken, src = (meta["taken"], "exif") if meta["taken"] else (_iso(info.st_mtime), "file")
                batch.append((rel, info.st_size, info.st_mtime, taken, src, meta["camera"],
                              has_jpeg, meta["error"]))
                if len(batch) >= BATCH:
                    flush()
        flush()

        # Files that vanished since the last scan
        gone = [(r,) for r in known if r not in seen]
        if gone:
            conn.executemany("DELETE FROM photos WHERE rel=?", gone)
        conn.commit()

        # Exact duplicates: only same-size files can be identical, so hash just those
        st["phase"] = "hashing"
        todo = conn.execute(
            "SELECT rel, size FROM photos WHERE sha256 IS NULL AND size IN "
            "(SELECT size FROM photos GROUP BY size HAVING COUNT(*) > 1)").fetchall()
        st["to_hash"] = len(todo)
        for rel, _size in todo:
            self._check()
            st["current"] = rel
            try:
                digest = sha256_file(str(root / rel), self._is_cancelled)
            except OSError:
                st["errors"] += 1
                continue
            conn.execute("UPDATE photos SET sha256=? WHERE rel=?", (digest, rel))
            st["hashed"] += 1
            if st["hashed"] % 20 == 0:
                conn.commit()
        conn.commit()

    def _is_cancelled(self) -> bool:
        return self._cancel

    # --- report ------------------------------------------------------------------------
    def report(self) -> dict:
        st = dict(self.state)
        out = {"state": st, "total_files": None}
        with self._db() as conn:
            count, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM photos").fetchone()
            if not count:
                return {**out, "total_files": 0 if st.get("finished") else None}
            first, last = conn.execute(
                "SELECT MIN(taken), MAX(taken) FROM photos WHERE taken_src='exif'").fetchone()
            cameras = conn.execute(
                "SELECT COALESCE(camera,'Unknown'), COUNT(*) FROM photos GROUP BY 1 "
                "ORDER BY 2 DESC").fetchall()
            years = conn.execute(
                "SELECT CASE WHEN taken_src='exif' THEN substr(taken,1,4) ELSE 'Unknown' END, "
                "COUNT(*) FROM photos GROUP BY 1 ORDER BY 1").fetchall()
            pairs = conn.execute("SELECT COUNT(*) FROM photos WHERE has_jpeg=1").fetchone()[0]
            file_dated = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE taken_src='file'").fetchone()[0]
            rels = [r[0] for r in conn.execute("SELECT rel FROM photos")]
            dup_rows = conn.execute(
                "SELECT sha256, COUNT(*), MAX(size) FROM photos WHERE sha256 IS NOT NULL "
                "GROUP BY sha256 HAVING COUNT(*) > 1 ORDER BY (COUNT(*)-1)*MAX(size) DESC").fetchall()
            groups = []
            for digest, n, sz in dup_rows[:25]:
                files = [r[0] for r in conn.execute(
                    "SELECT rel FROM photos WHERE sha256=? ORDER BY rel", (digest,))]
                groups.append({"count": n, "size_mb": round(sz / 1024 ** 2, 1), "files": files})
        folders = Counter(rel.rsplit("/", 1)[0] if "/" in rel else "(top folder)" for rel in rels)
        extra = sum(n - 1 for _d, n, _s in dup_rows)
        reclaim = sum((n - 1) * sz for _d, n, sz in dup_rows)
        out.update(
            total_files=count, total_gb=round(size / GB, 2), first=first, last=last,
            cameras=[{"name": c, "files": n} for c, n in cameras],
            years=[{"year": y, "files": n} for y, n in years],
            folders=[{"path": p, "files": n} for p, n in folders.most_common(15)],
            folder_count=len(folders), with_jpeg=pairs, dated_from_file=file_dated,
            duplicates={"groups": len(dup_rows), "extra_files": extra,
                        "reclaimable_gb": round(reclaim / GB, 2), "top": groups})
        return out
