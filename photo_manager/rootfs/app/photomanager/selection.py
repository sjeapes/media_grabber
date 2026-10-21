"""Upload a hand-picked set of photos: develop the ones that have no JPEG yet, then upload them all."""
from __future__ import annotations

import logging
import threading
import time

from .db import db
from .processor import DevelopError
from .uploader import UploadError

log = logging.getLogger("photomanager.selection")


class SelectionError(Exception):
    pass


class Selection:
    def __init__(self, cfg, processor, uploader, google):
        self.cfg, self.processor, self.uploader, self.google = cfg, processor, uploader, google
        self._lock = threading.Lock()
        self._cancel = False
        self.state = self._blank()

    @staticmethod
    def _blank():
        return {"running": False, "phase": None, "total": 0, "to_develop": 0, "developed": 0,
                "uploaded": 0, "failed": 0, "error": None, "stopped": None, "finished": None}

    def start(self, rels: list[str]) -> dict:
        if not rels:
            raise SelectionError("Nothing is selected")
        if not self.google.connected:
            raise SelectionError("Connect Google Photos first (Google Photos tab)")
        with self._lock:
            if self.state["running"] or self.processor.state["running"] or self.uploader.state["running"]:
                raise SelectionError("Developing or uploading is already running — try again when it has finished")
            with db(self.cfg) as conn:
                done = {r[0] for r in conn.execute(
                    "SELECT rel FROM developed WHERE status='done' AND out_rel IS NOT NULL AND error IS NULL")}
            todo = [r for r in rels if r not in done]
            self._cancel = False
            self.state = {**self._blank(), "running": True, "phase": "developing" if todo else "uploading",
                          "total": len(rels), "to_develop": len(todo)}
        threading.Thread(target=self._run, args=(list(rels), todo), daemon=True).start()
        return dict(self.state)

    def cancel(self):
        self._cancel = True
        self.processor.cancel()
        self.uploader.cancel()

    def _wait(self, state):
        while state["running"]:
            time.sleep(0.2)

    def _run(self, rels, todo):
        st = self.state
        try:
            if todo:
                try:
                    self.processor.start("manual", only=todo)
                except DevelopError as exc:
                    raise SelectionError(str(exc)) from None
                self._wait(self.processor.state)
                st["developed"] = self.processor.state["done"]
                if self._cancel:
                    st["stopped"] = "Stopped"
                    return
                if self.processor.state.get("stop_reason") or self.processor.state.get("error"):
                    st["stopped"] = self.processor.state.get("stop_text") or self.processor.state.get("error")
            st["phase"] = "uploading"
            try:
                self.uploader.start("manual", only=rels)
            except UploadError as exc:
                raise SelectionError(str(exc)) from None
            self._wait(self.uploader.state)
            ups = self.uploader.state
            st.update(uploaded=ups["done"], failed=ups["failed"])
            if ups.get("stop_reason") or ups.get("error"):
                st["stopped"] = ups.get("stop_text") or ups.get("error")
        except SelectionError as exc:
            st["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("selection upload failed")
            st["error"] = str(exc)
        finally:
            st.update(running=False, phase=None, finished=time.time())
