"""Download queue: persistent jobs, worker threads, progress parsing, cancel."""
from __future__ import annotations

import collections
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
SPEED = re.compile(r"@\s*([\d.]+\s*[kMG]?b(?:it)?/s|[\d.]+\s*[KMG]i?B/s)", re.I)
ETA = re.compile(r"ETA:?\s*([\d:]+)", re.I)
YTDLP_PROGRESS = re.compile(r"^MGPROGRESS\s+([\d.]+)%\s+(.*?)\s+ETA\s+(.*)$")
GIP_FILE = re.compile(r"INFO: (?:Downloaded|Recorded|File name prefix = )\s*:?\s*(.+)$")
YTDLP_DEST = re.compile(r'\[(?:download|Merger|ExtractAudio)\].*?(?:Destination: |into "|to: )"?(.+?)"?$')

ACTIVE = {"queued", "running"}


class Job(dict):
    """A job is a plain dict so it serialises straight to JSON."""


class JobQueue:
    def __init__(self, cfg, preflight, on_finish=None):
        """preflight(job) -> error string or None; checked again right before running."""
        self.cfg = cfg
        self.preflight = preflight
        self.on_finish = on_finish or (lambda job: None)
        self.path: Path = cfg.data_dir / "jobs.json"
        self.jobs: dict[str, Job] = collections.OrderedDict()
        self.q: queue.Queue[str] = queue.Queue()
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        self._dirty = False
        self._load()

    # --- persistence ----------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return
        try:
            for j in json.loads(self.path.read_text()):
                if j["status"] == "running":
                    j.update(status="failed", error="Interrupted by restart")
                self.jobs[j["id"]] = Job(j)
        except (OSError, ValueError, KeyError):
            return
        for j in self.jobs.values():
            if j["status"] == "queued":
                self.q.put(j["id"])

    def _save(self):
        with self.lock:
            # keep everything active plus the newest 200 finished jobs
            finished = [j for j in self.jobs.values() if j["status"] not in ACTIVE]
            keep = {j["id"] for j in finished[-200:]} | {
                j["id"] for j in self.jobs.values() if j["status"] in ACTIVE}
            for jid in [k for k in self.jobs if k not in keep]:
                del self.jobs[jid]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.jobs.values()), indent=1))
            tmp.replace(self.path)
            self._dirty = False

    def flusher(self):
        while True:
            time.sleep(2)
            if self._dirty:
                self._save()

    # --- api --------------------------------------------------------------
    def add(self, kind: str, title: str, cmd: list[str], meta: dict | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title, cmd=cmd, meta=meta or {},
                  status="queued", progress=0.0, speed=None, eta=None, error=None,
                  files=[], log=[], created=time.time(), started=None, finished=None)
        with self.lock:
            self.jobs[job["id"]] = job
            self._save()
        self.q.put(job["id"])
        return job

    def find_active(self, kind: str, key: str):
        with self.lock:
            for j in self.jobs.values():
                if j["kind"] == kind and j["status"] in ACTIVE and j["meta"].get("key") == key:
                    return j
        return None

    def list(self):
        with self.lock:
            return [self.public(j) for j in reversed(self.jobs.values())]

    @staticmethod
    def public(j):
        return {k: v for k, v in j.items() if k != "cmd"} | {"log": j["log"][-15:]}

    def cancel(self, jid: str) -> bool:
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j["status"] not in ACTIVE:
                return False
            j["status"] = "cancelled"
            j["finished"] = time.time()
            p = self.procs.get(jid)
        if p and p.poll() is None:
            p.terminate()
        self._save()
        return True

    def clear_finished(self):
        with self.lock:
            for jid in [k for k, j in self.jobs.items() if j["status"] not in ACTIVE]:
                del self.jobs[jid]
            self._save()

    def retry(self, jid: str):
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j["status"] in ACTIVE:
                return None
            return self.add(j["kind"], j["title"], j["cmd"], j["meta"])

    def counts(self):
        with self.lock:
            c = collections.Counter(j["status"] for j in self.jobs.values())
        return dict(c)

    # --- workers ------------------------------------------------------
    def start_workers(self, n: int):
        threading.Thread(target=self.flusher, daemon=True).start()
        for _ in range(n):
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            jid = self.q.get()
            with self.lock:
                j = self.jobs.get(jid)
                if not j or j["status"] != "queued":
                    continue
            err = self.preflight(j)
            with self.lock:
                if j["status"] != "queued":
                    continue
                if err:
                    j.update(status="failed", error=err, finished=time.time())
                    self._save()
                    self.on_finish(j)
                    continue
                j.update(status="running", started=time.time())
            self._run(j)

    def _run(self, j: Job):
        try:
            p = subprocess.Popen(j["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, errors="replace")
        except OSError as exc:
            j.update(status="failed", error=f"Could not start: {exc}", finished=time.time())
            self._save()
            self.on_finish(j)
            return
        self.procs[j["id"]] = p
        for raw in p.stdout:
            # get_iplayer redraws progress with \r; keep the last segment
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                if line:
                    self._parse(j, line)
        rc = p.wait()
        self.procs.pop(j["id"], None)
        with self.lock:
            if j["status"] == "cancelled":
                pass
            elif rc == 0 and not self._looks_failed(j):
                j.update(status="done", progress=100.0, eta=None)
            else:
                j.update(status="failed", error=j.get("error") or self._guess_error(j, rc))
            j["finished"] = time.time()
            self._save()
        self.on_finish(j)

    def _parse(self, j: Job, line: str):
        m = YTDLP_PROGRESS.match(line)
        if m:
            pct, speed, eta = (g.strip() for g in m.groups())
            j["progress"] = min(float(pct), 100.0)
            j["speed"] = None if speed in ("NA", "N/A", "Unknown", "Unknown B/s") else speed
            j["eta"] = None if eta in ("NA", "N/A", "Unknown") else eta
            self._dirty = True
            return
        if "%" in line and ("ETA" in line or " of " in line):
            m = PCT.findall(line)
            if m:
                j["progress"] = min(float(m[-1]), 100.0)
                s, e = SPEED.search(line), ETA.search(line)
                j["speed"] = s.group(1) if s else j["speed"]
                j["eta"] = e.group(1) if e else j["eta"]
                self._dirty = True
                return
        for rx in (GIP_FILE, YTDLP_DEST):
            m = rx.search(line)
            if m:
                f = m.group(1).strip()
                if f not in j["files"]:
                    j["files"].append(f)
        j["log"].append(line[-400:])
        del j["log"][:-60]
        self._dirty = True

    @staticmethod
    def _looks_failed(j: Job) -> bool:
        # get_iplayer exits 0 even when nothing could be fetched
        tail = " ".join(j["log"][-20:])
        return j["kind"] == "iplayer" and ("ERROR: Failed to record" in tail or
                                           "No media streams found" in tail)

    @staticmethod
    def _guess_error(j: Job, rc: int) -> str:
        tail = j["log"][-20:]
        joined = " ".join(tail)
        if "geoblock" in joined.lower() or "outside the uk" in joined.lower() or "403" in joined:
            return "Blocked by BBC — check the UK VPN (the server may be on a VPN blocklist)"
        for line in reversed(tail):
            if "ERROR" in line:
                return line[:300]
        return f"Exited with code {rc}"
