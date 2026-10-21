"""Shared helpers for the Photo Manager tests."""
import os
import struct
import time
from pathlib import Path


def build_cr2(make="Canon", model="Canon EOS 5D Mark III", taken="2024:05:01 10:00:00", pad=0):
    """A minimal little-endian TIFF with Make, Model and EXIF DateTimeOriginal — the same
    container (and tags) a real CR2 starts with, so the EXIF reader exercises its real path."""
    def ascii_(s):
        return s.encode() + b"\0"
    entries = []  # (tag, type, count, value-bytes-or-offset-callback)
    make_b, model_b, taken_b = ascii_(make), ascii_(model), ascii_(taken) if taken else b""
    n_ifd0 = 2 + (3 if taken else 2) * 12 + 4
    data_start = 8 + n_ifd0
    off_make = data_start
    off_model = off_make + len(make_b) + (len(make_b) % 2)
    off_exif = off_model + len(model_b) + (len(model_b) % 2)
    ifd0 = [(0x010F, 2, len(make_b), off_make), (0x0110, 2, len(model_b), off_model)]
    if taken:
        ifd0.append((0x8769, 4, 1, off_exif))
    out = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(ifd0))
    for tag, typ, cnt, val in ifd0:
        out += struct.pack("<HHII", tag, typ, cnt, val)
    out += struct.pack("<I", 0)
    out += make_b + b"\0" * (len(make_b) % 2) + model_b + b"\0" * (len(model_b) % 2)
    if taken:
        off_taken = off_exif + 2 + 12 + 4
        out += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(taken_b), off_taken)
        out += struct.pack("<I", 0) + taken_b
    return out + b"\xaa" * pad


def put(path: Path, data: bytes, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def wait_scan(c, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/status").json["scan"]
        if not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def choose_photos(env):
    r = env["c"].post("/api/folders", json={"photo": "Pictures/Canon"})
    assert r.status_code == 200, r.json
    return r


def run_scan(env):
    r = env["c"].post("/api/scan")
    assert r.status_code == 200, r.json
    st = wait_scan(env["c"])
    assert not st["error"], st["error"]
    return env["c"].get("/api/report").json


def tree(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime)
                  for p in root.rglob("*"))


