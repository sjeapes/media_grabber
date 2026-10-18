"""Download location and health.

Network storage added in Home Assistant (Settings → System → Storage) is mounted by the
Supervisor at /media/<name> (usage "Media") or /share/<name> (usage "Share"), and those
mounts are visible in this container's mount table. We detect them from there, so no
extra Supervisor permissions are needed and nothing is hard-coded.

Choice (persisted in /data/storage.json):
  "auto"   – use the only network storage; if there are several, ask the user to pick
  "<path>" – a specific network storage mount
  "local"  – Home Assistant's own disk (/media/MediaGrabber), with a free-space floor
We never silently fall back from a missing NAS to the local disk.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from pathlib import Path

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p"}
GB = 1024 ** 3
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="statvfs")


def read_mounts(mounts_file: str):
    out = []
    try:
        with open(mounts_file) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            out.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
    return out


def _disk_now(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {"total_gb": round(total / GB, 1), "free_gb": round(free / GB, 1),
            "used_gb": round(used / GB, 1),
            "used_pct": round(100 * used / total, 1) if total else None}


def disk(path, timeout: float = 5.0) -> dict:
    """statvfs with a timeout — a hung NFS/SMB server must not freeze the UI."""
    return _pool.submit(_disk_now, path).result(timeout)


class Storage:
    PROBE_EVERY = 60  # seconds between write tests

    def __init__(self, cfg, mounts_file: str = "/proc/self/mounts"):
        self.cfg = cfg
        self.mounts_file = mounts_file
        self.choice_file = cfg.data_dir / "storage.json"
        self._lock = threading.Lock()
        self._last_probe = 0.0
        self._probe_path = None
        self._probe = {"ok": None, "ms": None, "error": None, "at": None}
        self.choice = self._load_choice()

    # --- selection --------------------------------------------------------
    def _load_choice(self) -> str:
        try:
            return json.loads(self.choice_file.read_text()).get("choice") or "auto"
        except (OSError, ValueError):
            return "auto"

    def candidates(self, with_space: bool = False) -> list[dict]:
        """Network storage mounted under /media or /share (i.e. added in HA's Storage settings)."""
        roots = [(self.cfg.media_root, "Media"), (self.cfg.share_root, "Share")]
        found = {}
        for src, mnt, fstype in read_mounts(self.mounts_file):
            if fstype not in NETWORK_FS:
                continue
            p = Path(mnt)
            for root, usage in roots:
                if p != root and root in p.parents:
                    found[str(p)] = {"id": str(p), "path": str(p), "name": p.name, "usage": usage,
                                     "fstype": fstype, "source": src}
        cands = sorted(found.values(), key=lambda c: (c["usage"] != "Media", c["name"].lower()))
        if with_space:
            for c in cands:
                try:
                    c["free_gb"] = disk(c["path"], timeout=2)["free_gb"]
                except Exception:  # noqa: BLE001
                    c["free_gb"] = None
        return cands

    def set_choice(self, choice: str) -> dict:
        valid = {"auto", "local"} | {c["id"] for c in self.candidates()}
        if choice not in valid:
            raise ValueError("That location isn't available — is it mounted in "
                             "Settings → System → Storage?")
        tmp = self.choice_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"choice": choice}))
        tmp.replace(self.choice_file)
        self.choice = choice
        return self.status(force_probe=True)

    def resolve(self) -> dict:
        cands = self.candidates()
        res = {"choice": self.choice, "mode": None, "path": None, "name": None, "source": None,
               "fstype": None, "error": None, "needs_choice": False, "candidate_count": len(cands)}

        def use(c):
            res.update(mode="nas", path=c["path"], name=c["name"], source=c["source"],
                       fstype=c["fstype"])

        if self.choice == "local":
            res.update(mode="local", path=str(self.cfg.local_path), name="Home Assistant storage")
        elif self.choice != "auto":
            match = [c for c in cands if c["id"] == self.choice]
            if match:
                use(match[0])
            else:
                name = Path(self.choice).name
                res.update(mode="nas", path=self.choice, name=name, error=(
                    f"The selected network storage “{name}” isn't mounted — check it in "
                    "Settings → System → Storage, or choose another location"))
        else:
            legacy = self.cfg.options.get("nas_path")  # option from v0.1.x, used as a hint only
            preferred = [c for c in cands if legacy and c["path"] == legacy]
            if preferred:
                use(preferred[0])
            elif len(cands) == 1:
                use(cands[0])
            elif not cands:
                res["error"] = ("No network storage found — add your NAS in Settings → System → "
                                "Storage (usage: Media), or choose Home Assistant storage on the "
                                "Storage tab")
            else:
                res.update(needs_choice=True, error=(
                    f"{len(cands)} network storage locations found — choose one on the Storage tab"))
        self.cfg.base_path = Path(res["path"]) if res["path"] and not res["error"] else None
        return res

    # --- health -----------------------------------------------------------
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

    def status(self, force_probe: bool = False, probe: bool = True) -> dict:
        cfg = self.cfg
        r = self.resolve()
        low = float(cfg.opt("low_space_gb"))
        local_min = float(cfg.opt("local_min_free_gb"))
        out = {**r, "local_path": str(cfg.local_path), "low_space_gb": low,
               "local_min_free_gb": local_min, "disk": None, "probe": None, "folders": None,
               "healthy": False, "writable": False, "low_space": False,
               "problems": [], "warnings": [], "ha_disk": None,
               "network_mount": r["mode"] == "nas" and not r["error"]}
        try:
            out["ha_disk"] = disk(cfg.media_root if cfg.media_root.exists() else cfg.data_dir)
        except Exception:  # noqa: BLE001
            pass
        if r["error"]:
            out["problems"].append(r["error"])
            return out

        path = Path(r["path"])
        if r["mode"] == "local":
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                out["problems"].append(f"Cannot create {path}: {exc.strerror or exc}")
                return out
            out["warnings"].append("Saving to Home Assistant's own disk")
        out["folders"] = {"iplayer": str(cfg.iplayer_dir), "youtube": str(cfg.youtube_dir)}

        try:
            out["disk"] = disk(path)
        except concurrent.futures.TimeoutError:
            out["problems"].append("Storage isn't responding (free-space check timed out)")
            return out
        except OSError as exc:
            out["problems"].append(f"Cannot read free space: {exc.strerror or exc}")
            return out

        with self._lock:
            stale = time.time() - self._last_probe > self.PROBE_EVERY or self._probe_path != path
            if probe and (force_probe or stale):
                self._probe = {**self._write_probe(path), "at": time.time()}
                self._last_probe, self._probe_path = time.time(), path
            out["probe"] = dict(self._probe) if self._probe_path == path else None
        if out["probe"] and out["probe"]["ok"] is False:
            out["problems"].append(f"Write test failed: {out['probe']['error']}")

        free = out["disk"]["free_gb"]
        if r["mode"] == "local" and free < local_min:
            out["problems"].append(
                f"Home Assistant's disk has only {free} GB free — downloads here need at least "
                f"{local_min:g} GB free so Home Assistant itself keeps working")
        elif free < low:
            out["low_space"] = True
            out["warnings"].append(f"Only {free} GB free (warning below {low:g} GB)")

        out["healthy"] = not out["problems"]
        out["writable"] = out["healthy"] and free > 1
        return out

    def ensure_dirs(self):
        for d in (self.cfg.iplayer_dir, self.cfg.youtube_dir, self.cfg.youtube_dir / ".tmp"):
            d.mkdir(parents=True, exist_ok=True)

    def recent_files(self, limit: int = 60):
        if self.cfg.base_path is None:
            self.resolve()
        if self.cfg.base_path is None:
            return []
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
