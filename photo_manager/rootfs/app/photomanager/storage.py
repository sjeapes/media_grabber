"""NAS detection, folder settings and a safe folder browser.

Network storage added in Home Assistant (Settings → System → Storage) is mounted by the
Supervisor at /media/<name> (usage "Media") or /share/<name> (usage "Share"). Those mounts
are visible in this container's mount table, so we detect them from there: no extra
Supervisor permissions and nothing hard-coded.

Choice of NAS (persisted in /data/storage.json):
  "auto"   – use the only network storage; if there are several, ask the user to pick
  "<path>" – a specific network storage mount
We never fall back to Home Assistant's own disk: a missing NAS means "not ready".

Folders (persisted in /data/settings.json, stored relative to the NAS root so they
survive the NAS being remounted under a different name):
  photo_folder       – where the CR2 files live (chosen in the app; read-only)
  processed_folder   – where developed JPEGs will go (milestone 2)
  quarantine_folder  – where rejected photos will be moved (milestone 3)
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
from pathlib import Path, PurePosixPath

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p"}
GB = 1024 ** 3
# Folders NAS firmware creates that never hold real photos
SKIP_DIRS = {"@eadir", "#recycle", "@recycle", "@recently-snapshot", "$recycle.bin", ".trash",
             ".trashes", ".recycle", "#snapshot", ".snapshot", ".snapshots", "lost+found"}
DEFAULT_SETTINGS = {"photo_folder": None,
                    "processed_folder": "PhotoManager/Processed",
                    "quarantine_folder": "PhotoManager/Quarantine"}
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="fs")


class FolderError(ValueError):
    """A folder choice that can't be used; the message is shown to the user."""


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


def _first_entry(path):
    with os.scandir(path) as it:
        next(it, None)
    return True


def is_skipped(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIRS


def clean_rel(rel) -> str:
    """Normalise a user-supplied path relative to the NAS root. Raises FolderError."""
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    if not rel:
        return ""
    parts = PurePosixPath(rel).parts
    if any(p in ("..", ".") or p.startswith("/") for p in parts) or "\x00" in rel:
        raise FolderError("That folder path isn't valid")
    return "/".join(parts)


def _overlaps(a: str, b: str) -> bool:
    """True if folder a is the same as, inside, or contains folder b (relative paths)."""
    a, b = a.lower().split("/"), b.lower().split("/")
    n = min(len(a), len(b))
    return a[:n] == b[:n]


class Storage:
    def __init__(self, cfg, mounts_file: str = "/proc/self/mounts"):
        self.cfg = cfg
        self.mounts_file = mounts_file
        self.choice_file = cfg.data_dir / "storage.json"
        self.settings_file = cfg.data_dir / "settings.json"
        self._lock = threading.Lock()
        self.choice = self._load_choice()
        self.settings = self._load_settings()

    # --- persistence ----------------------------------------------------------
    @staticmethod
    def _write_json(path: Path, data: dict):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)

    def _load_choice(self) -> str:
        try:
            return json.loads(self.choice_file.read_text()).get("choice") or "auto"
        except (OSError, ValueError):
            return "auto"

    def _load_settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(self.settings_file.read_text())
            s.update({k: saved[k] for k in DEFAULT_SETTINGS if k in saved})
        except (OSError, ValueError):
            pass
        return s

    # --- NAS selection -----------------------------------------------------------
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
        valid = {"auto"} | {c["id"] for c in self.candidates()}
        if choice not in valid:
            raise FolderError("That location isn't available — is it mounted in "
                              "Settings → System → Storage?")
        self._write_json(self.choice_file, {"choice": choice})
        self.choice = choice
        return self.status()

    def resolve(self) -> dict:
        cands = self.candidates()
        res = {"choice": self.choice, "path": None, "name": None, "source": None,
               "fstype": None, "error": None, "needs_choice": False,
               "candidate_count": len(cands)}

        def use(c):
            res.update(path=c["path"], name=c["name"], source=c["source"], fstype=c["fstype"])

        if self.choice != "auto":
            match = [c for c in cands if c["id"] == self.choice]
            if match:
                use(match[0])
            else:
                name = Path(self.choice).name
                res.update(path=self.choice, name=name, error=(
                    f"The selected network storage “{name}” isn't mounted — check it in "
                    "Settings → System → Storage, or choose another location"))
        elif len(cands) == 1:
            use(cands[0])
        elif not cands:
            res["error"] = ("No network storage found — add your NAS in Settings → System → "
                            "Storage (usage: Media)")
        else:
            res.update(needs_choice=True, error=(
                f"{len(cands)} network storage locations found — choose one on the Storage tab"))
        self.cfg.base_path = Path(res["path"]) if res["path"] and not res["error"] else None
        return res

    # --- folders -----------------------------------------------------------------
    def abs_path(self, rel: str) -> Path:
        """Absolute path for a NAS-relative folder, guaranteed to stay inside the NAS."""
        if self.cfg.base_path is None:
            self.resolve()
        base = self.cfg.base_path
        if base is None:
            raise FolderError("No NAS is selected yet")
        rel = clean_rel(rel)
        p = base / rel if rel else base
        real_base, real_p = os.path.realpath(base), os.path.realpath(p)
        if real_p != real_base and not real_p.startswith(real_base + os.sep):
            raise FolderError("That folder is outside the NAS")
        return p

    def list_dirs(self, rel: str = "") -> dict:
        """Sub-folders of a NAS folder, for the folder picker. Cheap: one scandir."""
        self.resolve()
        rel = clean_rel(rel)
        path = self.abs_path(rel)

        def scan():
            dirs, cr2 = [], 0
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not is_skipped(e.name):
                                dirs.append(e.name)
                        elif e.name.lower().endswith(".cr2") and not e.name.startswith("."):
                            cr2 += 1
                    except OSError:
                        continue
            return sorted(dirs, key=str.lower), cr2

        try:
            dirs, cr2 = _pool.submit(scan).result(15)
        except concurrent.futures.TimeoutError:
            raise FolderError("The NAS isn't responding") from None
        except OSError as exc:
            raise FolderError(f"Can't open that folder: {exc.strerror or exc}") from None
        parent = "/".join(rel.split("/")[:-1]) if rel else None
        return {"rel": rel, "name": rel.rsplit("/", 1)[-1] if rel else self.cfg.base_path.name,
                "parent": parent, "dirs": dirs, "cr2_here": cr2,
                "is_root": rel == ""}

    def set_folders(self, photo=None, processed=None, quarantine=None) -> dict:
        """Validate and save any of the three folders (None = leave unchanged)."""
        new = dict(self.settings)
        if photo is not None:
            rel = clean_rel(photo)
            if not rel:
                raise FolderError("Choose a folder inside the NAS, not the whole share")
            if not self.abs_path(rel).is_dir():
                raise FolderError("That folder doesn't exist on the NAS")
            new["photo_folder"] = rel
        for key, val in (("processed_folder", processed), ("quarantine_folder", quarantine)):
            if val is not None:
                rel = clean_rel(val)
                if not rel:
                    raise FolderError("Choose a folder inside the NAS, not the whole share")
                new[key] = rel
        chosen = {k: v for k, v in new.items() if v}
        for a in chosen:
            for b in chosen:
                if a < b and _overlaps(chosen[a], chosen[b]):
                    raise FolderError(
                        f"The {a.replace('_folder', '').replace('_', ' ')} folder and the "
                        f"{b.replace('_folder', '').replace('_', ' ')} folder can't be the same "
                        "or inside each other")
        with self._lock:
            self._write_json(self.settings_file, new)
            self.settings = new
        return self.folders()

    def folders(self) -> dict:
        """The three folders as relative and absolute paths (absolute None if no NAS)."""
        out = {}
        for key in DEFAULT_SETTINGS:
            rel = self.settings.get(key)
            absolute = None
            if rel and self.cfg.base_path is not None:
                try:
                    absolute = str(self.abs_path(rel))
                except FolderError:
                    absolute = None
            out[key.replace("_folder", "")] = {"rel": rel, "path": absolute}
        return out

    def photo_dir(self) -> Path | None:
        """The photo folder if it's set and exists, else None."""
        rel = self.settings.get("photo_folder")
        if not rel or self.cfg.base_path is None:
            return None
        try:
            p = self.abs_path(rel)
        except FolderError:
            return None
        return p if p.is_dir() else None

    # --- health ----------------------------------------------------------------
    def status(self) -> dict:
        cfg = self.cfg
        r = self.resolve()
        low = float(cfg.opt("low_space_gb"))
        out = {**r, "low_space_gb": low, "disk": None, "problems": [], "warnings": [],
               "healthy": False, "low_space": False, "folders": self.folders(),
               "photo_folder_ok": False,
               "ready": False}
        if r["error"]:
            out["problems"].append(r["error"])
            return out
        path = Path(r["path"])
        try:
            out["disk"] = disk(path)
            _pool.submit(_first_entry, path).result(5)
        except concurrent.futures.TimeoutError:
            out["problems"].append("The NAS isn't responding (check timed out)")
            return out
        except OSError as exc:
            out["problems"].append(f"Cannot read the NAS: {exc.strerror or exc}")
            return out
        free = out["disk"]["free_gb"]
        if free < low:
            out["low_space"] = True
            out["warnings"].append(f"Only {free} GB free on the NAS (warning below {low:g} GB)")
        out["healthy"] = True
        if not self.settings.get("photo_folder"):
            out["warnings"].append("Choose your photo folder on the Storage tab")
        elif self.photo_dir() is None:
            out["problems"].append("The photo folder you chose isn't on this NAS any more — "
                                   "choose it again on the Storage tab")
            out["healthy"] = False
        else:
            out["photo_folder_ok"] = True
        out["ready"] = out["healthy"] and out["photo_folder_ok"]
        return out
