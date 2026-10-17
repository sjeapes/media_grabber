"""Flask app (served via HA ingress) wiring storage, VPN, queue, PVR and HA publishing."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .downloads import Downloads
from .ha import HAClient
from .jobs import JobQueue
from .storage import Storage
from .vpn import ConfigError, Vpn

log = logging.getLogger("mediagrabber")
STATIC = Path(__file__).parent / "static"


class Services:
    def __init__(self, cfg, mounts_file="/proc/self/mounts"):
        self.cfg = cfg
        self.storage = Storage(cfg, mounts_file)
        self.vpn = Vpn(cfg)
        self.dl = Downloads(cfg, self.vpn)
        self.ha = HAClient(cfg)
        self.last_finished = None
        self.versions = {}
        self.jobs = JobQueue(cfg, self.preflight, self.on_finish)
        self.next_pvr = None

    # --- checks -----------------------------------------------------------
    def iplayer_block_reason(self) -> str | None:
        if not self.cfg.opt("iplayer_requires_uk_vpn"):
            return None
        if not self.vpn.configured:
            return "UK VPN is not set up yet — add your WireGuard config on the VPN tab"
        if not self.vpn.running():
            return "UK VPN tunnel is not running — see the VPN tab"
        country = self.vpn.check_exit().get("country")
        if country != "GB":
            return f"VPN exit is {country or 'unknown'}, not the UK — iPlayer would refuse it"
        return None

    def preflight(self, job) -> str | None:
        nas = self.storage.status()
        if not nas["healthy"]:
            return "NAS not ready: " + "; ".join(nas["problems"])
        if not nas.get("writable"):
            return "NAS is full"
        try:
            self.storage.ensure_dirs()
        except OSError as exc:
            return f"Cannot create download folders: {exc}"
        if job["kind"] == "iplayer":
            return self.iplayer_block_reason()
        if job["kind"] == "link" and self.cfg.opt("vpn_for_youtube") and self.vpn.configured \
                and not self.vpn.running():
            return "VPN tunnel (used for links) is not running"
        return None

    def on_finish(self, job):
        self.last_finished = job
        self.ha.event("media_grabber_finished", {
            "title": job["title"], "status": job["status"], "kind": job["kind"],
            "error": job.get("error"), "files": job.get("files", [])})
        self.publish()

    def publish(self):
        try:
            self.ha.publish(self.storage.status(), self.vpn.status(), self.jobs.counts(),
                            self.last_finished)
        except Exception:  # noqa: BLE001
            log.exception("publish failed")

    # --- background loops -------------------------------------------------
    def publisher_loop(self):
        while True:
            self.publish()
            time.sleep(60)

    def queue_pvr(self) -> int:
        n = 0
        ids = []
        for entry in self.dl.pvr_list():
            key = f"pvr:{entry['id']}"
            if self.jobs.find_active("iplayer", key):
                continue
            self.jobs.add("iplayer", f"Series recording: {entry['name']}", self.dl.pvr_cmd(entry),
                          {"key": key, "pvr": entry["id"]})
            ids.append(entry["id"])
            n += 1
        if ids:
            self.dl.pvr_mark_run(ids)
        return n

    def pvr_loop(self):
        self.next_pvr = time.time() + 300  # first run five minutes after start-up
        while True:
            time.sleep(30)
            if time.time() < self.next_pvr:
                continue
            self.next_pvr = time.time() + 3600 * int(self.cfg.opt("pvr_interval_hours"))
            if self.iplayer_block_reason():
                log.info("PVR skipped: %s", self.iplayer_block_reason())
                continue
            self.dl.refresh_index()
            if self.dl.pvr_list():
                log.info("PVR queued %d series checks", self.queue_pvr())

    def update_tools(self):
        if self.cfg.opt("auto_update_tools") and shutil.which("pip"):
            for cmd in (["pip", "install", "-q", "-U", "yt-dlp[default]"],
                        ["/usr/local/bin/update_get_iplayer.sh"]):
                if Path(cmd[0]).exists() or shutil.which(cmd[0]):
                    try:
                        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                        log.info("%s -> %s", cmd[-1], (r.stdout or r.stderr).strip()[-200:])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("update %s failed: %s", cmd, exc)
        self.versions = self.tool_versions()

    def tool_versions(self) -> dict:
        v = {}
        try:
            v["yt-dlp"] = subprocess.run([self.cfg.yt_dlp, "--version"], capture_output=True,
                                         text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            v["yt-dlp"] = None
        gip = shutil.which(self.cfg.get_iplayer) or self.cfg.get_iplayer
        try:
            m = re.search(r"^my \$version = ([\d.]+);", Path(gip).read_text(errors="replace"), re.M)
            v["get_iplayer"] = m.group(1) if m else None
        except OSError:
            v["get_iplayer"] = None
        return v

    def start(self):
        threading.Thread(target=self.vpn.supervise, daemon=True).start()
        self.jobs.start_workers(int(self.cfg.opt("max_concurrent_downloads")))
        threading.Thread(target=self.publisher_loop, daemon=True).start()
        threading.Thread(target=self.pvr_loop, daemon=True).start()
        threading.Thread(target=self.update_tools, daemon=True).start()


def create_app(svc: Services) -> Flask:
    app = Flask(__name__, static_folder=None)
    cfg = svc.cfg

    @app.before_request
    def only_ingress():
        if request.path == "/healthz":
            return None
        if request.remote_addr not in cfg.allowed_clients:
            abort(403)
        return None

    def body():
        return request.get_json(silent=True) or {}

    def bad(msg, code=400):
        return jsonify({"error": msg}), code

    @app.get("/")
    def index():
        resp = send_from_directory(STATIC, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/api/status")
    def status():
        return jsonify({
            "nas": svc.storage.status(), "vpn": svc.vpn.status(), "index": svc.dl.index,
            "counts": svc.jobs.counts(), "versions": svc.versions,
            "iplayer_block": svc.iplayer_block_reason(), "next_pvr": svc.next_pvr,
            "ha_connected": svc.ha.enabled,
            "options": {k: cfg.opt(k) for k in ("tv_quality", "youtube_format",
                                                  "pvr_interval_hours", "vpn_for_youtube",
                                                  "iplayer_requires_uk_vpn", "low_space_gb")}})

    @app.post("/api/nas/check")
    def nas_check():
        return jsonify(svc.storage.status(force_probe=True))

    @app.get("/api/files")
    def files():
        return jsonify(svc.storage.recent_files())

    # --- iPlayer ------------------------------------------------------------
    @app.get("/api/iplayer/search")
    def search():
        reason = svc.iplayer_block_reason()
        if reason:
            return bad(reason, 409)
        try:
            return jsonify(svc.dl.search(request.args.get("q", ""), request.args.get("type", "tv")))
        except subprocess.TimeoutExpired:
            return bad("Search timed out — the programme index may still be building", 504)
        except RuntimeError as exc:
            return bad(str(exc), 502)

    @app.post("/api/iplayer/refresh")
    def refresh():
        reason = svc.iplayer_block_reason()
        if reason:
            return bad(reason, 409)
        if svc.dl.index["running"]:
            return jsonify(svc.dl.index)
        threading.Thread(target=svc.dl.refresh_index, daemon=True).start()
        return jsonify({**svc.dl.index, "running": True})

    @app.post("/api/iplayer/download")
    def iplayer_download():
        b = body()
        try:
            target = svc.dl.normalise_iplayer_target(b.get("target", ""))
        except ValueError as exc:
            return bad(str(exc))
        if svc.jobs.find_active("iplayer", target):
            return bad("Already in the queue", 409)
        title = (b.get("title") or target)[:200]
        job = svc.jobs.add("iplayer", title, svc.dl.iplayer_cmd(
            target, series=bool(b.get("series")), subtitles=bool(b.get("subtitles"))),
            {"key": target, "series": bool(b.get("series"))})
        return jsonify(svc.jobs.public(job)), 201

    # --- links (yt-dlp) -----------------------------------------------------
    @app.post("/api/link/download")
    def link_download():
        b = body()
        try:
            url = svc.dl.check_url(b.get("url", ""))
        except ValueError as exc:
            return bad(str(exc))
        if svc.jobs.find_active("link", url):
            return bad("Already in the queue", 409)
        job = svc.jobs.add("link", url, svc.dl.ytdlp_cmd(
            url, audio_only=bool(b.get("audio_only")), playlist=bool(b.get("playlist"))),
            {"key": url, "audio_only": bool(b.get("audio_only"))})
        return jsonify(svc.jobs.public(job)), 201

    # --- queue ------------------------------------------------------------
    @app.get("/api/jobs")
    def jobs():
        return jsonify(svc.jobs.list())

    @app.post("/api/jobs/<jid>/cancel")
    def cancel(jid):
        return jsonify({"ok": svc.jobs.cancel(jid)})

    @app.post("/api/jobs/<jid>/retry")
    def retry(jid):
        job = svc.jobs.retry(jid)
        return (jsonify(svc.jobs.public(job)), 201) if job else bad("Cannot retry that job", 409)

    @app.post("/api/jobs/clear")
    def clear():
        svc.jobs.clear_finished()
        return jsonify({"ok": True})

    # --- PVR ----------------------------------------------------------------
    @app.get("/api/pvr")
    def pvr_list():
        return jsonify(svc.dl.pvr_list())

    @app.post("/api/pvr")
    def pvr_add():
        b = body()
        try:
            item = svc.dl.pvr_add(b.get("name", ""), b.get("query", ""), b.get("type", "tv"),
                                  bool(b.get("subtitles")))
        except ValueError as exc:
            return bad(str(exc))
        return jsonify(item), 201

    @app.delete("/api/pvr/<pid>")
    def pvr_delete(pid):
        return jsonify({"ok": svc.dl.pvr_delete(pid)})

    @app.post("/api/pvr/run")
    def pvr_run():
        reason = svc.iplayer_block_reason()
        if reason:
            return bad(reason, 409)
        return jsonify({"queued": svc.queue_pvr()})

    # --- VPN ------------------------------------------------------------
    @app.get("/api/vpn")
    def vpn_get():
        return jsonify(svc.vpn.status())

    @app.post("/api/vpn")
    def vpn_set():
        text = body().get("config", "")
        try:
            svc.vpn.save_config(text)
        except ConfigError as exc:
            return bad(f"That doesn't look like a WireGuard config: {exc}")
        return jsonify(svc.vpn.status())

    @app.delete("/api/vpn")
    def vpn_delete():
        svc.vpn.delete_config()
        return jsonify(svc.vpn.status())

    @app.post("/api/vpn/check")
    def vpn_check():
        svc.vpn.check_exit(force=True)
        return jsonify(svc.vpn.status())

    return app
