"""NAS detection, folder settings and a safe folder browser.

Network storage added in Home Assistant (Settings → System → Storage) is mounted by the
Supervisor at /media/<name> (usage "Media") or /share/<name> (usage "Share"). Those mounts
are visible in this container's mount table, so we detect them from there: no extra
Supervisor permissions and nothing hard-coded.

Choice of NAS (persisted in /data/storage.json):
  "auto"   – use the only network storage; if there are several, ask the user to pick
  "<path>" – a specific network storage mount
We never fall back to Home Assistant's own disk: a missing NAS means "not ready".

Settings (persisted in /data/settings.json; folders are stored relative to the NAS root so
they survive the NAS being remounted under a different name):
  photo_folder       – where the CR2 files live (chosen in the app)
  processed_folder   – where developed JPEGs go when output_mode is "separate"
  quarantine_folder  – where photos you choose to move are put (never deleted)
  processing_enabled – developing RAW files is OFF until the user turns it on
  output_mode        – "separate" (a mirror of the photo folder under processed_folder) or
                       "alongside" (each JPEG saved next to its RAW file)
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
from pathlib import Path, PurePosixPath

from . import dtsettings

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p"}
GB = 1024 ** 3
# Folders NAS firmware creates that never hold real photos
SKIP_DIRS = {"@eadir", "#recycle", "@recycle", "@recently-snapshot", "$recycle.bin", ".trash",
             ".trashes", ".recycle", "#snapshot", ".snapshot", ".snapshots", "lost+found"}
FOLDER_KEYS = ("photo_folder", "processed_folder", "quarantine_folder")
OUTPUT_MODES = ("separate", "alongside")
DEFAULT_SETTINGS = {"photo_folder": None,
                    "processed_folder": "PhotoManager/Processed",
                    "quarantine_folder": "PhotoManager/Quarantine",
                    "processing_enabled": False,
                    "output_mode": "separate",
                    "develop_scope": "",          # sub-folder of the photo folder ("" = all of it)
                    "skip_with_jpeg": False,       # leave RAWs that already have a camera JPEG
                    "google_upload": False,        # automatically upload developed JPEGs to Google Photos
                    "takeout_folder": None,        # where the Google Takeout export was put
                    "dt": {}}                      # darktable options (see dtsettings.py)
DEVELOPED_SUFFIX = "_developed"
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
            s["processing_enabled"] = s["processing_enabled"] is True   # anything odd means off
            if s["output_mode"] not in OUTPUT_MODES:
                s["output_mode"] = "separate"
            s["skip_with_jpeg"] = s["skip_with_jpeg"] is True
            s["google_upload"] = s["google_upload"] is True
            s["dt"] = dtsettings.load(s["dt"])
            if not isinstance(s["takeout_folder"], str):
                s["takeout_folder"] = None
            try:
                s["develop_scope"] = clean_rel(s["develop_scope"]) if isinstance(
                    s["develop_scope"], str) else ""
            except FolderError:
                s["develop_scope"] = ""
        except (OSError, ValueError):
            pass
        s["dt"] = dtsettings.load(s.get("dt"))       # complete, even when nothing was saved yet
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

    @staticmethod
    def _check_overlaps(settings: dict):
        """The photo, quarantine (and, in 'separate' mode, developed) folders must be distinct."""
        labels = {"photo_folder": "photo", "processed_folder": "developed photos",
                  "quarantine_folder": "quarantine"}
        used = [k for k in FOLDER_KEYS if settings.get(k)
                and not (k == "processed_folder" and settings.get("output_mode") == "alongside")]
        for i, a in enumerate(used):
            for b in used[i + 1:]:
                if _overlaps(settings[a], settings[b]):
                    raise FolderError(f"The {labels[a]} folder and the {labels[b]} folder can't be "
                                      "the same or inside each other")

    def set_folders(self, photo=None, processed=None, quarantine=None) -> dict:
        """Validate and save any of the three folders (None = leave unchanged)."""
        new = dict(self.settings)
        if photo is not None:
            rel = clean_rel(photo)
            if not rel:
                raise FolderError("Choose a folder inside the NAS, not the whole share")
            if not self.abs_path(rel).is_dir():
                raise FolderError("That folder doesn't exist on the NAS")
            if rel != new["photo_folder"]:
                new["develop_scope"] = ""         # the old test folder belonged to the old photo folder
            new["photo_folder"] = rel
        for key, val in (("processed_folder", processed), ("quarantine_folder", quarantine)):
            if val is not None:
                rel = clean_rel(val)
                if not rel:
                    raise FolderError("Choose a folder inside the NAS, not the whole share")
                new[key] = rel
        self._check_overlaps(new)
        self._save_settings(new)
        return self.folders()

    def set_dt(self, changes: dict) -> dict:
        """Change darktable options (validated). Returns the full set."""
        try:
            dt = dtsettings.clean(changes, self.settings["dt"])
        except dtsettings.DtError as exc:
            raise FolderError(str(exc)) from None
        self._save_settings({**self.settings, "dt": dt})
        return dt

    def set_takeout_folder(self, rel) -> str | None:
        if rel is None or rel == "":
            self._save_settings({**self.settings, "takeout_folder": None})
            return None
        rel = clean_rel(rel)
        if not rel:
            raise FolderError("Choose a folder inside the NAS, not the whole share")
        if not self.abs_path(rel).is_dir():
            raise FolderError("That folder doesn't exist on the NAS")
        self._save_settings({**self.settings, "takeout_folder": rel})
        return rel

    def set_processing(self, enabled=None, output_mode=None, scope=None,
                       skip_with_jpeg=None) -> dict:
        """Turn developing on/off, choose where the JPEGs go, which folder to develop, and
        whether RAWs that already have a camera JPEG are skipped."""
        new = dict(self.settings)
        if skip_with_jpeg is not None:
            if not isinstance(skip_with_jpeg, bool):
                raise FolderError("Invalid setting")
            new["skip_with_jpeg"] = skip_with_jpeg
        if scope is not None:
            if not isinstance(scope, str):
                raise FolderError("Invalid folder")
            rel = clean_rel(scope)
            if rel:
                root = self.photo_dir()
                if root is None:
                    raise FolderError("Choose your photo folder first")
                real_root, real = os.path.realpath(root), os.path.realpath(root / rel)
                if not real.startswith(real_root + os.sep) or not os.path.isdir(real):
                    raise FolderError("That folder isn't inside your photo folder")
            new["develop_scope"] = rel
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise FolderError("Invalid setting")
            new["processing_enabled"] = enabled
        if output_mode is not None:
            if output_mode not in OUTPUT_MODES:
                raise FolderError("Choose where to save the JPEGs")
            new["output_mode"] = output_mode
        self._check_overlaps(new)
        self._save_settings(new)
        return {"enabled": new["processing_enabled"], "output_mode": new["output_mode"],
                "scope": new["develop_scope"], "skip_with_jpeg": new["skip_with_jpeg"]}

    def set_google_upload(self, enabled) -> bool:
        if not isinstance(enabled, bool):
            raise FolderError("Invalid setting")
        self._save_settings({**self.settings, "google_upload": enabled})
        return enabled

    def _save_settings(self, new: dict):
        with self._lock:
            self._write_json(self.settings_file, new)
            self.settings = new

    def developed_rel(self, rel: str) -> str:
        """Where the developed JPEG for a RAW file goes, relative to the NAS root.

        The name always ends in _developed.jpg so it can never collide with the camera's own
        JPEG (IMG_0001.JPG) saved next to the RAW file."""
        p = PurePosixPath(clean_rel(rel))
        name = p.stem + DEVELOPED_SUFFIX + ".jpg"
        if self.settings["output_mode"] == "alongside":
            base = PurePosixPath(self.settings["photo_folder"] or "")
        else:
            base = PurePosixPath(self.settings["processed_folder"] or "")
        return str(base / p.parent / name)

    def folders(self) -> dict:
        """The three folders as relative and absolute paths (absolute None if no NAS)."""
        out = {}
        for key in FOLDER_KEYS:
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
               "processing_enabled": self.settings["processing_enabled"],
               "output_mode": self.settings["output_mode"],
               "develop_scope": self.settings["develop_scope"],
               "skip_with_jpeg": self.settings["skip_with_jpeg"],
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
