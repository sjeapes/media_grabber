"""Small preview pictures of photos, taken from the JPEG that every CR2 file carries inside it.

Reading that preview is quick and needs no RAW converter. Previews are shrunk and cached in
the add-on's private data folder, so the NAS is only read the first time a photo is shown.
Only photos Photo Manager knows about can be previewed, never arbitrary paths.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
from pathlib import Path

from .db import db

try:  # Pillow shrinks the preview; without it the embedded preview is sent as it is
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

MAX_EMBEDDED = 3 * 1024 * 1024   # larger previews than this aren't sent unshrunk
SIZE = 480
ROTATE = {3: 180, 6: 270, 8: 90}   # TIFF orientation tag -> degrees to rotate (counter-clockwise)


def _read_ifds(f, endian: str, off: int) -> list[dict]:
    ifds, seen = [], set()
    while off and off not in seen and len(ifds) < 6:
        seen.add(off)
        f.seek(off)
        raw = f.read(2)
        if len(raw) < 2:
            break
        n = struct.unpack(endian + "H", raw)[0]
        body = f.read(12 * n + 4)
        if n > 200 or len(body) < 12 * n + 4:
            break
        tags = {}
        for i in range(n):
            tag, typ, cnt = struct.unpack(endian + "HHI", body[i * 12:i * 12 + 8])
            val = (struct.unpack(endian + "H", body[i * 12 + 8:i * 12 + 10])[0] if typ == 3
                   else struct.unpack(endian + "I", body[i * 12 + 8:i * 12 + 12])[0])
            tags[tag] = (val, cnt)
        ifds.append(tags)
        off = struct.unpack(endian + "I", body[12 * n:12 * n + 4])[0]
    return ifds


def embedded_preview(path) -> tuple[bytes, int] | None:
    """(JPEG bytes, orientation) of the best preview inside a CR2/TIFF, or the file itself if it
    is a small JPEG. None if there isn't one."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:2] == b"\xff\xd8":
                f.seek(0)
                return (f.read(), 1) if size <= MAX_EMBEDDED else None
            endian = {b"II": "<", b"MM": ">"}.get(head[:2])
            if not endian or struct.unpack(endian + "H", head[2:4])[0] != 42:
                return None
            ifds = _read_ifds(f, endian, struct.unpack(endian + "I", head[4:8])[0])
            orientation = ifds[0].get(0x0112, (1, 1))[0] if ifds else 1
            cands = []
            for t in ifds:
                if 0x0201 in t and 0x0202 in t:
                    cands.append((t[0x0201][0], t[0x0202][0]))
                elif 0x0111 in t and 0x0117 in t and t.get(0x0103, (0,))[0] in (6, 7) \
                        and t[0x0111][1] == 1:
                    cands.append((t[0x0111][0], t[0x0117][0]))
            good = []
            for off, length in cands:
                if length < 100 or off + length > size:
                    continue
                f.seek(off)
                data = f.read(length)
                if data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-16:]:
                    good.append(data)
            if not good:
                return None
            small = [d for d in good if len(d) <= MAX_EMBEDDED]
            return (max(small, key=len) if small else min(good, key=len)), orientation
    except (OSError, struct.error):
        return None


def make_thumbnail(path) -> bytes | None:
    found = embedded_preview(path)
    if not found:
        return None
    data, orientation = found
    if Image is None:
        return data if len(data) <= MAX_EMBEDDED else None
    try:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (SIZE * 2, SIZE * 2))
        img = img.convert("RGB")
        if ROTATE.get(orientation):
            img = img.rotate(ROTATE[orientation], expand=True)
        img.thumbnail((SIZE, SIZE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - a damaged preview just means no picture
        return None


class Thumbs:
    def __init__(self, cfg, storage):
        self.cfg, self.storage = cfg, storage

    @property
    def cache_dir(self) -> Path:
        return self.cfg.data_dir / "thumbs"

    def _get(self, path: Path) -> bytes | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
        cached = self.cache_dir / f"{key}.jpg"
        try:
            return cached.read_bytes()
        except OSError:
            pass
        data = make_thumbnail(path)
        if data:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, cached)
            except OSError:
                pass
        return data

    def library(self, rel: str) -> bytes | None:
        """A photo in the library (must be a file the last scan found)."""
        root = self.storage.photo_dir()
        if root is None:
            return None
        with db(self.cfg) as conn:
            if not conn.execute("SELECT 1 FROM photos WHERE rel=?", (rel,)).fetchone():
                return None
        return self._get(root / rel)

    def quarantined(self, id_: int) -> bytes | None:
        with db(self.cfg) as conn:
            row = conn.execute("SELECT q_rel FROM quarantined WHERE id=? AND restored_at IS NULL",
                               (id_,)).fetchone()
        if not row:
            return None
        try:
            return self._get(self.storage.abs_path(row[0]))
        except Exception:  # noqa: BLE001
            return None
