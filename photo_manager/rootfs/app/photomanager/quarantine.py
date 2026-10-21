"""Moving duplicate photos to a quarantine folder — never deleting anything.

You choose which copy of a duplicate group goes to quarantine; at least one copy always stays.
Before anything moves, both files are read again and their CRC32 checksums compared. The
move is refused unless every file to be moved is identical to the copy that stays (same
size, same CRC32, and unchanged since the last scan). That check runs on the server every
time, so it can't be skipped from the web page.

A quarantined file keeps its folder structure inside the quarantine folder, and the
Quarantine tab can put it back where it came from.
"""
from __future__ import annotations

import errno
import os
import shutil
import time
import zlib
from pathlib import Path, PurePosixPath

from .db import db
from .storage import FolderError, clean_rel

CHUNK = 4 * 1024 * 1024
MAX_MOVE = 25            # files per request


class QuarantineError(ValueError):
    """Why a move or restore was refused; the message is shown to the user."""


def crc32_file(path) -> str:
    """CRC32 of a whole file as 8 upper-case hex digits (the usual way checksums are shown)."""
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08X}"


def _unique(path: Path) -> Path:
    """path, or 'name (2).ext', 'name (3).ext'… so nothing in quarantine is overwritten."""
    if not path.exists():
        return path
    for n in range(2, 10000):
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
    raise QuarantineError("Too many files with that name in the quarantine folder")


class Quarantine:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage

    # --- duplicate groups ---------------------------------------------------------------
    def groups(self, offset: int = 0, limit: int = 10) -> dict:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 50))
        with db(self.cfg) as conn:
            heads = conn.execute(
                "SELECT sha256, COUNT(*), MAX(size) FROM photos WHERE sha256 IS NOT NULL "
                "GROUP BY sha256 HAVING COUNT(*) > 1 ORDER BY (COUNT(*)-1)*MAX(size) DESC, sha256")\
                .fetchall()
            out = []
            for sha, n, size in heads[offset:offset + limit]:
                files = [{"rel": r, "taken": t, "size": s} for r, t, s in conn.execute(
                    "SELECT rel, taken, size FROM photos WHERE sha256=? ORDER BY rel", (sha,))]
                out.append({"id": sha, "count": n, "size": size, "files": files})
        return {"total": len(heads), "offset": offset, "groups": out}

    # --- checks ---------------------------------------------------------------------------
    def _rows(self, keep: str, move: list[str]) -> tuple[dict, list[dict]]:
        """Validate the request against the library. Raises QuarantineError."""
        if not isinstance(move, list) or not move:
            raise QuarantineError("Tick at least one copy to move")
        if len(move) > MAX_MOVE:
            raise QuarantineError(f"Move at most {MAX_MOVE} files at a time")
        try:
            keep = clean_rel(keep)
            move = [clean_rel(m) for m in move]
        except FolderError:
            raise QuarantineError("That file path isn't valid") from None
        if len(set(move)) != len(move):
            raise QuarantineError("The same file was chosen twice")
        if not keep:
            raise QuarantineError("At least one copy has to stay")
        if keep in move:
            raise QuarantineError("The copy that stays can't also be moved")
        cols = "rel, size, mtime, sha256, taken, taken_src, camera"
        with db(self.cfg) as conn:
            found = {r[0]: dict(zip(("rel", "size", "mtime", "sha256", "taken", "taken_src",
                                     "camera"), r)) for r in conn.execute(
                f"SELECT {cols} FROM photos WHERE rel IN ({','.join('?' * (len(move) + 1))})",
                [keep, *move])}
        for rel in [keep, *move]:
            if rel not in found:
                raise QuarantineError(f"“{rel}” isn't in the library — scan again")
        group = {r["sha256"] for r in found.values()}
        if None in group or len(group) != 1:
            raise QuarantineError("Those files aren't a duplicate group — scan again")
        return found[keep], [found[m] for m in move]

    def verify(self, keep: str, move: list[str]) -> dict:
        """Read the kept copy and each file to move, and compare their CRC32 checksums."""
        keep_row, move_rows = self._rows(keep, move)
        root = self.storage.photo_dir()
        if root is None:
            raise QuarantineError("The photo folder isn't available — check the Storage tab")

        def check(row: dict) -> dict:
            path = root / row["rel"]
            res = {"rel": row["rel"], "size": None, "crc32": None, "problem": None}
            try:
                st = path.stat()
                res["size"] = st.st_size
                if st.st_size != row["size"] or st.st_mtime != row["mtime"]:
                    res["problem"] = "Changed since the last scan — scan again"
                    return res
                res["crc32"] = crc32_file(path)
            except OSError as exc:
                res["problem"] = f"Can't read the file: {exc.strerror or exc}"
            return res

        kept = check(keep_row)
        files = []
        for row in move_rows:
            res = check(row)
            res["match"] = (res["problem"] is None and kept["problem"] is None
                            and res["size"] == kept["size"] and res["crc32"] == kept["crc32"])
            if res["problem"] is None and kept["problem"] is None and not res["match"]:
                res["problem"] = "Different from the copy that stays"
            files.append(res)
        return {"keep": kept, "files": files,
                "all_match": kept["problem"] is None and all(f["match"] for f in files)}

    # --- moving -----------------------------------------------------------------------------
    def _destination(self, rel: str) -> Path:
        folder = self.storage.settings.get("quarantine_folder")
        if not folder:
            raise QuarantineError("Choose a quarantine folder on the Storage tab first")
        dest = self.storage.abs_path(f"{folder}/{rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        return _unique(dest)

    @staticmethod
    def _relocate(src: Path, dest: Path, crc: str | None = None):
        """Rename src to dest; if they're on different volumes, copy, check, then remove src."""
        try:
            os.rename(src, dest)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        crc = crc or crc32_file(src)
        shutil.copy2(src, dest)
        if crc32_file(dest) != crc:
            dest.unlink()
            raise QuarantineError("The copy in quarantine didn't match, so nothing was moved")
        src.unlink()

    def move(self, keep: str, move: list[str]) -> dict:
        """Verify again, then move the chosen copies to quarantine. All or nothing on the check."""
        result = self.verify(keep, move)       # always re-checked here, whatever the page showed
        if not result["all_match"]:
            bad = [f["rel"] for f in result["files"] if not f["match"]]
            why = result["keep"]["problem"] or next(
                (f["problem"] for f in result["files"] if f["problem"]), None)
            raise QuarantineError(
                "Nothing was moved: the checksums don't all match"
                + (f" ({why})" if why else "") + (f" — {bad[0]}" if bad else ""))
        keep_row, rows = self._rows(keep, move)
        root = self.storage.photo_dir()
        by_rel = {f["rel"]: f for f in result["files"]}
        moved, errors = [], []
        for row in rows:
            src = root / row["rel"]
            try:
                dest = self._destination(row["rel"])
                self._relocate(src, dest, by_rel[row["rel"]]["crc32"])
            except (OSError, QuarantineError, FolderError) as exc:
                errors.append({"rel": row["rel"], "error": getattr(exc, "strerror", None) or str(exc)})
                continue
            q_rel = os.path.relpath(dest, self.cfg.base_path).replace(os.sep, "/")
            with db(self.cfg) as conn:
                conn.execute("DELETE FROM photos WHERE rel=?", (row["rel"],))
                conn.execute(
                    "INSERT INTO quarantined (rel,q_rel,keep_rel,size,mtime,crc32,sha256,taken,"
                    "taken_src,camera,moved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row["rel"], q_rel, keep_row["rel"], row["size"], row["mtime"],
                     by_rel[row["rel"]]["crc32"], row["sha256"], row["taken"], row["taken_src"],
                     row["camera"], time.time()))
            moved.append({"rel": row["rel"], "q_rel": q_rel})
        return {"moved": moved, "errors": errors, "crc32": result["keep"]["crc32"],
                "kept": keep_row["rel"]}

    # --- the quarantine list ----------------------------------------------------------------
    def listing(self) -> dict:
        with db(self.cfg) as conn:
            rows = conn.execute(
                "SELECT id, rel, q_rel, keep_rel, size, crc32, taken, moved_at FROM quarantined "
                "WHERE restored_at IS NULL ORDER BY moved_at DESC, id DESC").fetchall()
        base = self.cfg.base_path
        items, total, present_n = [], 0, 0
        for id_, rel, q_rel, keep, size, crc, taken, at in rows:
            present = bool(base) and (base / q_rel).is_file()
            total += size or 0 if present else 0
            present_n += int(present)
            items.append({"id": id_, "rel": rel, "q_rel": q_rel, "keep_rel": keep, "size": size,
                          "crc32": crc, "taken": taken, "moved_at": at, "present": present})
        return {"folder": self.storage.settings.get("quarantine_folder"), "items": items,
                "files": present_n, "bytes": total}

    def restore(self, id_: int) -> dict:
        with db(self.cfg) as conn:
            row = conn.execute(
                "SELECT rel, q_rel, size, mtime, taken, taken_src, camera FROM quarantined "
                "WHERE id=? AND restored_at IS NULL", (id_,)).fetchone()
        if not row:
            raise QuarantineError("That file isn't in quarantine any more")
        rel, q_rel, size, mtime, taken, taken_src, camera = row
        root = self.storage.photo_dir()
        if root is None:
            raise QuarantineError("The photo folder isn't available — check the Storage tab")
        src = self.storage.abs_path(q_rel)
        if not src.is_file():
            raise QuarantineError("The file is no longer in the quarantine folder")
        dest = root / PurePosixPath(rel)
        if dest.exists():
            raise QuarantineError("A file with that name is already in the photo folder")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._relocate(src, dest)
        except OSError as exc:
            raise QuarantineError(f"Can't put it back: {exc.strerror or exc}") from None
        with db(self.cfg) as conn:
            conn.execute("UPDATE quarantined SET restored_at=? WHERE id=?", (time.time(), id_))
            conn.execute(
                "INSERT OR REPLACE INTO photos (rel,size,mtime,taken,taken_src,camera,has_jpeg,"
                "sha256,exif_error) VALUES (?,?,?,?,?,?,0,NULL,NULL)",
                (rel, size, os.stat(dest).st_mtime, taken, taken_src, camera))
        return {"restored": rel}

    def forget_missing(self) -> int:
        """Drop list entries whose file you've since emptied from the quarantine folder."""
        base = self.cfg.base_path
        n = 0
        with db(self.cfg) as conn:
            for id_, q_rel in conn.execute(
                    "SELECT id, q_rel FROM quarantined WHERE restored_at IS NULL").fetchall():
                if not base or not (base / q_rel).exists():
                    conn.execute("DELETE FROM quarantined WHERE id=?", (id_,))
                    n += 1
        return n
