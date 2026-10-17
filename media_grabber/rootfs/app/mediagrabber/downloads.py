"""get_iplayer and yt-dlp command building, iPlayer search/index and PVR."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid

FIELDS = ["pid", "name", "episode", "channel", "duration", "available", "thumbnail", "type", "desc"]
LISTFORMAT = "MGROW|" + "|".join(f"<{f}>" for f in FIELDS)
PID_RX = re.compile(r"^[a-z0-9]{8}$")
IPLAYER_URL = re.compile(r"^https?://(www\.)?bbc\.co\.uk/(iplayer|sounds|programmes)/", re.I)
HTTP_URL = re.compile(r"^https?://[^\s]+$", re.I)
# Hosts whose streams are DRM-protected; yt-dlp cannot (and this app will not) fetch them.
DRM_HOSTS = ("itv.com", "channel4.com", "channel5.com", "my5.tv", "stv.tv", "netflix.com",
             "disneyplus.com", "primevideo.com", "nowtv.com")


class Downloads:
    def __init__(self, cfg, vpn):
        self.cfg = cfg
        self.vpn = vpn
        self.pvr_file = cfg.data_dir / "pvr.json"
        self.index = {"running": False, "last_ok": None, "last_error": None, "started": None}
        self._index_lock = threading.Lock()

    # --- get_iplayer ------------------------------------------------------
    def _gip_base(self) -> list[str]:
        cmd = [self.cfg.get_iplayer, "--profile-dir", str(self.cfg.gip_profile), "--nocopyright"]
        if self.vpn.configured:
            cmd += ["--proxy", self.cfg.proxy_url]
        return cmd

    def _gip_output(self, subtitles: bool) -> list[str]:
        out = ["--output", str(self.cfg.iplayer_dir), "--subdir", "--whitespace",
               "--tv-quality", self.cfg.opt("tv_quality")]
        if subtitles:
            out.append("--subtitles")
        return out

    @staticmethod
    def normalise_iplayer_target(target: str) -> str:
        t = target.strip()
        if PID_RX.match(t) or IPLAYER_URL.match(t):
            return t
        raise ValueError("Enter an iPlayer/Sounds link or an 8-character programme ID")

    def iplayer_cmd(self, target: str, series: bool = False, subtitles: bool = False) -> list[str]:
        cmd = self._gip_base() + self._gip_output(subtitles) + ["--pid", target]
        if series:
            cmd.append("--pid-recursive")
        return cmd

    def pvr_cmd(self, entry: dict) -> list[str]:
        return (self._gip_base() + self._gip_output(entry.get("subtitles", False)) +
                ["--type", entry.get("type", "tv"), "--expiry", "99999999",
                 "--get", entry["query"]])

    def search(self, query: str, type_: str = "tv", timeout: int = 180) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        types = {"tv": "tv", "radio": "radio", "all": "tv,radio"}.get(type_, "tv")
        cmd = self._gip_base() + ["--type", types, "--expiry", "99999999",
                                  "--listformat", LISTFORMAT, "--sort", "name", query]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        rows = []
        for line in res.stdout.splitlines():
            if not line.startswith("MGROW|"):
                continue
            vals = line[6:].split("|", len(FIELDS) - 1)
            if len(vals) != len(FIELDS):
                continue
            row = dict(zip(FIELDS, vals))
            try:
                row["minutes"] = round(int(row["duration"]) / 60) if row["duration"] else None
            except ValueError:
                row["minutes"] = None
            rows.append(row)
        if not rows and res.returncode not in (0, None):
            err = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["search failed"]
            raise RuntimeError(err[0])
        return rows[:200]

    def refresh_index(self, timeout: int = 1800):
        """Rebuild get_iplayer's programme index (needs the UK tunnel)."""
        if not self._index_lock.acquire(blocking=False):
            return False
        try:
            self.index.update(running=True, started=time.time())
            cmd = self._gip_base() + ["--refresh", "--type", "tv,radio"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
            if res.returncode == 0:
                self.index.update(last_ok=time.time(), last_error=None)
            else:
                tail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["refresh failed"]
                self.index["last_error"] = tail[0][:300]
            return True
        except subprocess.TimeoutExpired:
            self.index["last_error"] = "Index refresh timed out"
            return True
        finally:
            self.index["running"] = False
            self._index_lock.release()

    # --- yt-dlp -------------------------------------------------------------
    @staticmethod
    def check_url(url: str) -> str:
        url = url.strip()
        if not HTTP_URL.match(url):
            raise ValueError("Paste a full http(s) link")
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower()
        if any(host == d or host.endswith("." + d) for d in DRM_HOSTS):
            raise ValueError("That service uses DRM-protected streams, so it can't be downloaded")
        return url

    def ytdlp_cmd(self, url: str, audio_only=False, playlist=False) -> list[str]:
        ydir = self.cfg.youtube_dir
        cmd = [self.cfg.yt_dlp, "--newline", "--no-colors", "--ignore-config",
               "--progress-template",
               "download:MGPROGRESS %(progress._percent_str)s %(progress._speed_str)s ETA %(progress._eta_str)s",
               "-P", f"home:{ydir}", "-P", f"temp:{ydir / '.tmp'}",
               "-o", "%(uploader|Unknown)s/%(title).150B [%(id)s].%(ext)s",
               "--embed-metadata", "--embed-chapters", "--embed-thumbnail",
               "--download-archive", str(self.cfg.data_dir / "ytdlp-archive.txt"),
               "--yes-playlist" if playlist else "--no-playlist"]
        if audio_only:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            cmd += ["-f", self.cfg.opt("youtube_format"), "--merge-output-format", "mp4"]
        if self.cfg.opt("vpn_for_youtube") and self.vpn.configured:
            cmd += ["--proxy", self.cfg.proxy_url]
        return cmd + ["--", url]

    # --- PVR ------------------------------------------------------------
    def pvr_list(self) -> list[dict]:
        if not self.pvr_file.exists():
            return []
        try:
            return json.loads(self.pvr_file.read_text())
        except ValueError:
            return []

    def _pvr_save(self, items):
        self.pvr_file.write_text(json.dumps(items, indent=1))

    def pvr_add(self, name: str, query: str, type_: str = "tv", subtitles=False) -> dict:
        query = query.strip()
        if len(query) < 3:
            raise ValueError("Search term too short — it would match too many programmes")
        item = {"id": uuid.uuid4().hex[:8], "name": name.strip() or query, "query": query,
                "type": type_ if type_ in ("tv", "radio") else "tv", "subtitles": bool(subtitles),
                "created": time.time(), "last_run": None}
        items = self.pvr_list()
        items.append(item)
        self._pvr_save(items)
        return item

    def pvr_delete(self, pid: str) -> bool:
        items = self.pvr_list()
        new = [i for i in items if i["id"] != pid]
        self._pvr_save(new)
        return len(new) != len(items)

    def pvr_mark_run(self, ids):
        items = self.pvr_list()
        for i in items:
            if i["id"] in ids:
                i["last_run"] = time.time()
        self._pvr_save(items)
