"""NAS health: mounted, writable, free space, and the HA disk for comparison."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p"}
GB = 1024 ** 3


def _mount_for(path: Path, mounts_file: str = "/proc/self/mounts"):
    """Return (mountpoint, fstype, source) of the longest mount prefix covering path."""
    best = None
    target = str(path.resolve()) if path.exists() else str(path)
    try:
        with open(mounts_file) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
                if target == mnt or target.startswith(mnt.rstrip("/") + "/"):
                    if best is None or len(mnt) > len(best[0]):
                        best = (mnt, fstype, src)
    except OSError:
        pass
    return best


def _disk(path: Path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {
        "total_gb": round(total / GB, 1),
        "free_gb": round(free / GB, 1),
        "used_gb": round(used / GB, 1),
        "used_pct": round(100 * used / total, 1) if total else None,
    }


class Storage:
    PROBE_EVERY = 60  # seconds between write tests

    def __init__(self, cfg, mounts_file: str = "/proc/self/mounts"):
        self.cfg = cfg
        self.mounts_file = mounts_file
        self._lock = threading.Lock()
        self._last_probe = 0.0
        self._probe = {"ok": None, "ms": None, "error": None, "at": None}

    def _write_probe(self, path: Path):
        probe = path / ".media_grabber_probe"
        t0 = time.monotonic()
        try:
            with open(probe, "wb") as fh:
                fh.write(os.urandom(1024 * 1024))
                fh.flush()
                os.fsync(fh.fileno())
            with open(probe, "rb") as fh:
                fh.read()
            probe.unlink()
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "error": None}
        except OSError as exc:
            return {"ok": False, "ms": None, "error": f"{exc.strerror or exc}"}

    def status(self, force_probe: bool = False) -> dict:
        cfg = self.cfg
        path = cfg.nas_path
        low = float(cfg.opt("low_space_gb"))
        out = {"path": str(path), "exists": path.is_dir(), "mounted": False,
               "network_mount": False, "fstype": None, "source": None,
               "disk": None, "probe": None, "low_space_gb": low,
               "healthy": False, "problems": [], "ha_disk": None,
               "folders": {"iplayer": str(cfg.iplayer_dir), "youtube": str(cfg.youtube_dir)}}
        try:
            out["ha_disk"] = _disk(cfg.data_dir)
        except OSError:
            pass

        if not out["exists"]:
            out["problems"].append(f"{path} does not exist — add the NAS in Settings → System → Storage (usage: Media)")
            return out

        m = _mount_for(path, self.mounts_file)
        if m:
            mnt, fstype, src = m
            out["mounted"] = os.path.normpath(mnt) == os.path.normpath(str(path))
            out["fstype"], out["source"] = fstype, src
            out["network_mount"] = out["mounted"] and fstype in NETWORK_FS
        if cfg.opt("require_network_mount") and not out["network_mount"]:
            out["problems"].append(
                "NAS is not mounted here — refusing to write so the Home Assistant disk doesn't fill up")

        try:
            out["disk"] = _disk(path)
        except OSError as exc:
            out["problems"].append(f"Cannot read free space: {exc.strerror or exc}")

        with self._lock:
            if force_probe or time.time() - self._last_probe > self.PROBE_EVERY:
                if not out["problems"]:
                    self._probe = {**self._write_probe(path), "at": time.time()}
                self._last_probe = time.time()
            out["probe"] = dict(self._probe)
        if out["probe"]["ok"] is False:
            out["problems"].append(f"Write test failed: {out['probe']['error']}")

        if out["disk"] and out["disk"]["free_gb"] < low:
            out["low_space"] = True
            out["problems"].append(f"Only {out['disk']['free_gb']} GB free (warning below {low:g} GB)")
        else:
            out["low_space"] = False

        # Low space is a warning; everything else blocks downloads.
        blocking = [p for p in out["problems"] if not p.startswith("Only ")]
        out["healthy"] = not blocking
        out["writable"] = out["healthy"] and (out["disk"] or {}).get("free_gb", 0) > 1
        return out

    def ensure_dirs(self):
        for d in (self.cfg.iplayer_dir, self.cfg.youtube_dir, self.cfg.youtube_dir / ".tmp"):
            d.mkdir(parents=True, exist_ok=True)

    def recent_files(self, limit: int = 60):
        exts = {".mp4", ".mkv", ".m4a", ".mp3", ".webm", ".opus", ".ts", ".srt"}
        files = []
        for root_name, root in (("iPlayer", self.cfg.iplayer_dir), ("YouTube", self.cfg.youtube_dir)):
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for f in filenames:
                    p = Path(dirpath) / f
                    if p.suffix.lower() in exts and not f.startswith("."):
                        try:
                            st = p.stat()
                        except OSError:
                            continue
                        files.append({"source": root_name, "name": f,
                                      "folder": str(p.parent.relative_to(root)) if p.parent != root else "",
                                      "size_mb": round(st.st_size / 1024 ** 2, 1), "mtime": st.st_mtime})
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return files[:limit]
