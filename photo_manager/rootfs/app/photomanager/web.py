"""Flask app (served via HA ingress) wiring storage, scanner and HA publishing."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .ha import HAClient
from .scanner import Scanner
from .storage import FolderError, Storage

log = logging.getLogger("photomanager")
STATIC = Path(__file__).parent / "static"


class Services:
    def __init__(self, cfg, mounts_file="/proc/self/mounts"):
        self.cfg = cfg
        self.storage = Storage(cfg, mounts_file)
        self.scanner = Scanner(cfg, on_done=self.publish)
        self.ha = HAClient(cfg)

    def publish(self):
        try:
            self.ha.publish(self.storage.status(), self.scanner.report())
        except Exception:  # noqa: BLE001
            log.exception("publish failed")

    def publisher_loop(self):
        while True:
            self.publish()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.publisher_loop, daemon=True).start()


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
        return jsonify({"nas": svc.storage.status(), "scan": dict(svc.scanner.state),
                        "ha_connected": svc.ha.enabled})

    # --- NAS and folders -------------------------------------------------------
    @app.get("/api/storage/options")
    def storage_options():
        return jsonify({"choice": svc.storage.choice,
                        "candidates": svc.storage.candidates(with_space=True)})

    @app.post("/api/storage/select")
    def storage_select():
        try:
            st = svc.storage.set_choice(str(body().get("choice", "")))
        except FolderError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(st)

    @app.get("/api/folders")
    def folders_list():
        try:
            return jsonify(svc.storage.list_dirs(request.args.get("rel", "")))
        except FolderError as exc:
            return bad(str(exc))

    @app.post("/api/folders")
    def folders_set():
        b = body()
        try:
            svc.storage.set_folders(photo=b.get("photo"), processed=b.get("processed"),
                                    quarantine=b.get("quarantine"))
        except FolderError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(svc.storage.status())

    # --- scan ------------------------------------------------------------------
    @app.post("/api/scan")
    def scan_start():
        svc.storage.resolve()
        root = svc.storage.photo_dir()
        if root is None:
            return bad("Choose your photo folder on the Storage tab first")
        if not svc.scanner.start(root):
            return bad("A scan is already running", 409)
        return jsonify(dict(svc.scanner.state))

    @app.post("/api/scan/cancel")
    def scan_cancel():
        svc.scanner.cancel()
        return jsonify({"ok": True})

    @app.get("/api/report")
    def report():
        return jsonify(svc.scanner.report())

    return app
