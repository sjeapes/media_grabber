"""Read-only disk-usage scan of a storage location (what's using the space?).

Walks the tree once in a background thread, aggregating sizes per folder. Folders
smaller than KEEP_BYTES are folded into their parent so the saved tree stays small
even on a NAS with millions of files. Nothing is ever modified or deleted.
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("mediagrabber.usage")
GB = 1024 ** 3
KEEP_BYTES = 50 * 1024 ** 2          # keep folders >= 50 MB as their own tree nodes
TOP_FILES = 60
RECYCLE_NAMES = {"#recycle", "@recycle", "@recently-snapshot", "$recycle.bin", ".trash",
                 ".trashes", ".recycle", "@eadir", "#snapshot", ".snapshot", ".snapshots"}


class Cancelled(Exception):
    pass


def gb(n: int) -> float:
    return round(n / GB, 2)


class UsageScanner:
    def __init__(self, cfg, on_done=None):
        self.cfg = cfg
        self.on_done = on_done or (lambda: None)
        self.file = cfg.data_dir / "usage.json"
        self._lock = threading.Lock()
        self._cancel = False
        self.state = {"running": False, "path": None, "started": None, "finished": None,
                      "files": 0, "dirs": 0, "bytes": 0, "current": None, "errors": 0,
                      "error": None, "cancelled": False, "disk": None}
        self.nodes: dict[str, dict] = {}
        self.largest: list = []
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self):
        try:
            d = json.loads(self.file.read_text())
            self.state.update(d["state"], running=False)
            self.nodes, self.largest = d["nodes"], d["largest_files"]
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": self.state, "nodes": self.nodes,
                                   "largest_files": self.largest}))
        tmp.replace(self.file)

    # --- scanning --------------------------------------------------------
    def start(self, path: str, disk: dict | None = None) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self._cancel = False
            self.state = {"running": True, "path": path, "started": time.time(), "finished": None,
                          "files": 0, "dirs": 0, "bytes": 0, "current": "", "errors": 0,
                          "error": None, "cancelled": False, "disk": disk}
        threading.Thread(target=self._run, args=(Path(path),), daemon=True).start()
        return True

    def cancel(self):
        self._cancel = True

    def _run(self, root: Path):
        nodes: dict[str, dict] = {}
        heap: list = []
        try:
            root_dev = os.stat(root).st_dev
            self._scan(root, "", root_dev, nodes, heap)
            self.state["finished"] = time.time()
        except Cancelled:
            self.state.update(cancelled=True, finished=time.time())
        except Exception as exc:  # noqa: BLE001
            log.exception("usage scan failed")
            self.state.update(error=str(exc), finished=time.time())
        self.state["running"] = False
        self.state["current"] = None
        if not self.state["cancelled"] and not self.state["error"]:
            self.nodes = nodes
            self.largest = [{"rel": r, "s": s} for s, r in sorted(heap, reverse=True)]
            self._save()
        self.on_done()

    def _scan(self, path: Path, rel: str, root_dev: int, nodes: dict, heap: list):
        size = files = own = 0
        children = []
        try:
            it = os.scandir(path)
        except OSError:
            self.state["errors"] += 1
            return 0, 0
        with it:
            for e in it:
                if self._cancel:
                    raise Cancelled
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.stat(follow_symlinks=False).st_dev != root_dev:
                            continue  # another mount inside this one (e.g. a NAS under /media)
                        crel = f"{rel}/{e.name}" if rel else e.name
                        s, f = self._scan(Path(e.path), crel, root_dev, nodes, heap)
                        size += s
                        files += f
                        if s >= KEEP_BYTES:
                            children.append(crel)
                    else:
                        sz = e.stat(follow_symlinks=False).st_size
                        size += sz
                        own += sz
                        files += 1
                        self.state["files"] += 1
                        self.state["bytes"] += sz
                        item = (sz, f"{rel}/{e.name}" if rel else e.name)
                        if len(heap) < TOP_FILES:
                            heapq.heappush(heap, item)
                        elif sz > heap[0][0]:
                            heapq.heapreplace(heap, item)
                except OSError:
                    self.state["errors"] += 1
        self.state["dirs"] += 1
        self.state["current"] = rel
        if size >= KEEP_BYTES or rel == "":
            nodes[rel] = {"s": size, "f": files, "o": own, "c": children}
        return size, files

    # --- queries ---------------------------------------------------------
    @staticmethod
    def flag(rel: str) -> str | None:
        name = rel.rsplit("/", 1)[-1].lower()
        if name in RECYCLE_NAMES:
            return "recycle"
        return None

    def children(self, rel: str = "") -> dict | None:
        node = self.nodes.get(rel)
        if node is None:
            return None
        kids = sorted(({"rel": c, "name": c.rsplit("/", 1)[-1], "s": self.nodes[c]["s"],
                        "f": self.nodes[c]["f"], "has_children": bool(self.nodes[c]["c"]),
                        "flag": self.flag(c)}
                       for c in node["c"] if c in self.nodes), key=lambda k: -k["s"])
        small = node["s"] - node["o"] - sum(k["s"] for k in kids)
        return {"rel": rel, "s": node["s"], "f": node["f"], "own_files": node["o"],
                "small_folders": max(small, 0), "children": kids}

    def summary(self) -> dict:
        st = dict(self.state)
        root = self.nodes.get("")
        out = {"state": st, "total": root["s"] if root else None}
        if not root:
            return out
        top = self.children("")["children"]
        depth2 = sorted(((r, n["s"]) for r, n in self.nodes.items() if r.count("/") == 1),
                        key=lambda x: -x[1])
        recycle = [(r, n["s"]) for r, n in self.nodes.items() if self.flag(r)]
        out.update(
            top_level=[{"name": k["name"], "gb": gb(k["s"]), "files": k["f"]} for k in top[:20]],
            biggest_subfolders=[{"path": r, "gb": gb(s)} for r, s in depth2[:20]],
            largest_files=[{"path": x["rel"], "gb": gb(x["s"])} for x in self.largest[:20]],
            recycle_bins=[{"path": r, "gb": gb(s)} for r, s in sorted(recycle, key=lambda x: -x[1])],
        )
        return out
