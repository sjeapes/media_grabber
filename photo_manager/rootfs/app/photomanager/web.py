"""Flask app (served via HA ingress) wiring storage, scanner, developing, quarantine and HA."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from .gphotos import GoogleError, GooglePhotos
from .gallery import Gallery
from .selection import Selection, SelectionError
from .takeout import Takeout
from .ha import HAClient
from .processor import DevelopError, Processor
from .quarantine import Quarantine, QuarantineError
from .scanner import Scanner
from .storage import FolderError, Storage
from . import thumbs as thumbs_mod
from .thumbs import Thumbs
from .uploader import UploadError, Uploader

log = logging.getLogger("photomanager")
STATIC = Path(__file__).parent / "static"


class Services:
    def __init__(self, cfg, mounts_file="/proc/self/mounts"):
        self.cfg = cfg
        self.storage = Storage(cfg, mounts_file)
        self.scanner = Scanner(cfg, on_done=self.publish)
        self.processor = Processor(cfg, self.storage, self.scanner, on_done=self.on_develop_done)
        self.quarantine = Quarantine(cfg, self.storage)
        self.thumbs = Thumbs(cfg, self.storage)
        self.gallery = Gallery(cfg)
        self.takeout = Takeout(cfg)
        self.google = GooglePhotos(cfg)
        self.uploader = Uploader(cfg, self.storage, self.google, on_done=self.publish)
        self.selection = Selection(cfg, self.processor, self.uploader, self.google)
        self.ha = HAClient(cfg)

    def publish(self):
        try:
            self.ha.publish(self.storage.status(), self.scanner.report(),
                            self.processor.summary(), self.quarantine.listing(),
                            self.uploader.summary())
        except Exception:  # noqa: BLE001
            log.exception("publish failed")

    def on_develop_done(self):
        st = self.processor.state
        self.ha.event("photo_manager_develop_finished", {
            "trigger": st.get("trigger"), "developed": st.get("done"), "failed": st.get("failed"),
            "stopped": st.get("stop_text"), "error": st.get("error")})
        try:                        # send the fresh JPEGs to Google Photos if that is switched on
            if self.uploader.enabled and self.google.connected:
                self.uploader.start("auto")
        except UploadError:
            pass
        self.publish()

    def publisher_loop(self):
        while True:
            self.publish()
            time.sleep(60)

    def develop_loop(self):
        while True:
            time.sleep(30)
            try:
                if self.processor.scheduled_tick():
                    log.info("Started tonight's develop run")
                elif self.uploader.scheduled_tick(self.processor.state["running"]):
                    log.info("Started uploading waiting photos to Google Photos")
            except Exception:  # noqa: BLE001
                log.exception("develop schedule check failed")

    def start(self):
        threading.Thread(target=self.publisher_loop, daemon=True).start()
        threading.Thread(target=self.develop_loop, daemon=True).start()

    def busy_reason(self) -> str | None:
        """Files must not move while a scan or develop run is reading the photo folder."""
        if self.scanner.state["running"]:
            return "A scan is running — try again when it has finished"
        if self.processor.state["running"]:
            return "Developing is running — try again when it has finished or stop it first"
        return None


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
        resp = send_from_directory(STATIC, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/api/status")
    def status():
        dv = svc.processor.state
        return jsonify({"nas": svc.storage.status(), "scan": dict(svc.scanner.state),
                        "develop": {"running": dv["running"], "done": dv["done"],
                                    "total": dv["total"], "phase": dv["phase"],
                                    "enabled": svc.storage.settings["processing_enabled"]},
                        "upload": {"running": svc.uploader.state["running"],
                                   "done": svc.uploader.state["done"],
                                   "total": svc.uploader.state["total"],
                                   "enabled": svc.storage.settings["google_upload"],
                                   "connected": svc.google.connected},
                        "takeout": {"scanning": svc.takeout.state["running"],
                                    "job": svc.selection.state["running"]},
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
        if svc.processor.state["running"]:
            return bad("Developing is running — it scans first by itself", 409)
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

    # --- developing ------------------------------------------------------------
    @app.get("/api/develop")
    def develop_get():
        return jsonify(svc.processor.summary())

    @app.post("/api/develop/settings")
    def develop_settings():
        b = body()
        try:
            svc.storage.set_processing(
                enabled=b.get("enabled"), output_mode=b.get("output_mode"),
                scope=b.get("scope"), skip_with_jpeg=b.get("skip_with_jpeg"))
        except FolderError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(svc.processor.summary())

    @app.get("/api/thumb")
    def thumb():
        size = thumbs_mod.SIZE_LARGE if request.args.get("size") == "l" else thumbs_mod.SIZE
        try:
            if request.args.get("q"):
                data = svc.thumbs.quarantined(int(request.args["q"]), size)
            elif request.args.get("dev"):                      # the developed JPEG of a photo
                data = svc.thumbs.developed(request.args["dev"], size)
            else:
                data = svc.thumbs.library(request.args.get("rel", ""), size)
        except (TypeError, ValueError):
            data = None
        if not data:
            abort(404)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/gallery")
    def gallery_list():
        a = request.args
        try:
            res = svc.gallery.list(a.get("filter", "all"), a.get("folder", ""), a.get("q", ""),
                                   a.get("days", 7, type=int), a.get("offset", 0, type=int),
                                   a.get("limit", 60, type=int))
        except (TypeError, ValueError):
            return bad("Invalid request")
        res["counts"] = svc.gallery.counts(a.get("days", 7, type=int))
        return jsonify(res)

    @app.post("/api/develop/dt")
    def develop_dt():
        try:
            svc.storage.set_dt(body())
        except FolderError as exc:
            return bad(str(exc))
        return jsonify(svc.processor.summary())

    @app.post("/api/develop/preview")
    def develop_preview():
        b = body()
        try:
            svc.processor.start_preview(str(b.get("rel") or ""), b.get("dt") or {})
        except DevelopError as exc:
            return bad(str(exc), 409 if "already" in str(exc) or "running" in str(exc) else 400)
        return jsonify(svc.processor.preview_state)

    @app.get("/api/develop/preview")
    def develop_preview_state():
        return jsonify(svc.processor.preview_state)

    @app.get("/api/develop/preview.jpg")
    def develop_preview_image():
        path = svc.processor.preview_path
        if svc.processor.preview_state.get("error") or not path.is_file():
            abort(404)
        return Response(path.read_bytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/develop/run")
    def develop_run():
        try:
            svc.processor.start("manual")
        except DevelopError as exc:
            return bad(str(exc), 409 if "already" in str(exc) else 400)
        return jsonify(svc.processor.summary())

    @app.post("/api/develop/cancel")
    def develop_cancel():
        svc.processor.cancel()
        return jsonify({"ok": True})

    @app.post("/api/develop/retry-failed")
    def develop_retry():
        return jsonify({"reset": svc.processor.retry_failed()})

    # --- duplicates and quarantine ----------------------------------------------------
    @app.get("/api/duplicates")
    def duplicates():
        try:
            return jsonify(svc.quarantine.groups(request.args.get("offset", 0, type=int),
                                                 request.args.get("limit", 10, type=int)))
        except (TypeError, ValueError):
            return bad("Invalid paging")

    def move_args():
        b = body()
        return str(b.get("keep") or ""), b.get("move")

    @app.post("/api/quarantine/verify")
    def quarantine_verify():
        keep, move = move_args()
        try:
            return jsonify(svc.quarantine.verify(keep, move))
        except QuarantineError as exc:
            return bad(str(exc))

    @app.post("/api/quarantine/move")
    def quarantine_move():
        busy = svc.busy_reason()
        if busy:
            return bad(busy, 409)
        keep, move = move_args()
        try:
            res = svc.quarantine.move(keep, move)
        except QuarantineError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(res)

    @app.get("/api/quarantine")
    def quarantine_list():
        return jsonify(svc.quarantine.listing())

    @app.post("/api/quarantine/restore")
    def quarantine_restore():
        busy = svc.busy_reason()
        if busy:
            return bad(busy, 409)
        try:
            res = svc.quarantine.restore(int(body().get("id", 0)))
        except (QuarantineError, TypeError, ValueError) as exc:
            return bad(str(exc) if isinstance(exc, QuarantineError) else "Invalid request")
        svc.publish()
        return jsonify(res)

    @app.post("/api/quarantine/forget-missing")
    def quarantine_forget():
        return jsonify({"forgotten": svc.quarantine.forget_missing()})

    # --- Google Takeout: what is missing from Google Photos --------------------------------------
    def takeout_root():
        rel = svc.storage.settings["takeout_folder"]
        if not rel:
            return None
        try:
            p = svc.storage.abs_path(rel)
        except FolderError:
            return None
        return p if p.is_dir() else None

    @app.get("/api/takeout")
    def takeout_get():
        return jsonify({"folder": svc.storage.settings["takeout_folder"], "folder_ok": takeout_root() is not None,
                        "state": dict(svc.takeout.state), "counts": svc.takeout.counts(),
                        "job": dict(svc.selection.state), "google_connected": svc.google.connected,
                        "photo_folder": svc.storage.settings["photo_folder"]})

    @app.post("/api/takeout/folder")
    def takeout_folder():
        try:
            svc.storage.set_takeout_folder(body().get("folder"))
        except FolderError as exc:
            return bad(str(exc))
        return takeout_get()

    @app.post("/api/takeout/scan")
    def takeout_scan():
        try:
            if not svc.takeout.start(takeout_root()):
                return bad("A Takeout scan is already running", 409)
        except ValueError as exc:
            return bad(str(exc))
        return jsonify(dict(svc.takeout.state))

    @app.post("/api/takeout/cancel")
    def takeout_cancel():
        svc.takeout.cancel()
        return jsonify({"ok": True})

    @app.get("/api/takeout/missing")
    def takeout_missing():
        a = request.args
        if not svc.takeout.counts()["entries"]:
            return bad("Scan your Takeout export first")
        return jsonify(svc.takeout.report(a.get("folder", ""), a.get("possible") == "1",
                                          a.get("offset", 0, type=int), a.get("limit", 60, type=int)))

    @app.post("/api/takeout/upload")
    def takeout_upload():
        b = body()
        rels = svc.takeout.resolve(b.get("rels"), b.get("folders"), b.get("possible") is True)
        try:
            res = svc.selection.start(rels)
        except SelectionError as exc:
            return bad(str(exc), 409 if "already" in str(exc) else 400)
        return jsonify(res)

    @app.post("/api/takeout/upload/cancel")
    def takeout_upload_cancel():
        svc.selection.cancel()
        return jsonify({"ok": True})

    # --- Google Photos ---------------------------------------------------------------------
    @app.get("/api/google")
    def google_get():
        return jsonify(svc.uploader.summary())

    @app.post("/api/google/auth-url")
    def google_auth_url():
        try:
            return jsonify({"url": svc.google.auth_url()})
        except GoogleError as exc:
            return bad(str(exc))

    @app.post("/api/google/connect")
    def google_connect():
        try:
            svc.google.connect(str(body().get("redirect_url") or ""))
        except GoogleError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(svc.uploader.summary())

    @app.post("/api/google/disconnect")
    def google_disconnect():
        svc.google.disconnect()
        try:
            svc.storage.set_google_upload(False)
        except FolderError:
            pass
        svc.publish()
        return jsonify(svc.uploader.summary())

    @app.post("/api/google/settings")
    def google_settings():
        enabled = body().get("upload")
        if enabled is True and not svc.google.connected:
            return bad("Connect Google Photos first")
        try:
            svc.storage.set_google_upload(enabled)
        except FolderError as exc:
            return bad(str(exc))
        svc.publish()
        return jsonify(svc.uploader.summary())

    @app.post("/api/google/run")
    def google_run():
        try:
            svc.uploader.start("manual")
        except UploadError as exc:
            return bad(str(exc), 409 if "already" in str(exc) else 400)
        return jsonify(svc.uploader.summary())

    @app.post("/api/google/cancel")
    def google_cancel():
        svc.uploader.cancel()
        return jsonify({"ok": True})

    @app.post("/api/google/retry-failed")
    def google_retry():
        return jsonify({"reset": svc.uploader.retry_failed()})

    return app
