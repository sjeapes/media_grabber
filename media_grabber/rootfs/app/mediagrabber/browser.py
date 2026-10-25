"""Remote browser: a headless Chromium inside the add-on, routed through the UK tunnel.

The Browse tab shows its screen (Chrome DevTools screencast, JPEG frames) and sends
clicks, scrolls and keys back. Your own device never talks to the BBC, so it works
from anywhere. Chromium only runs while someone is looking at the tab.
"""
from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

import websocket  # websocket-client

log = logging.getLogger("mediagrabber.browser")
HOME = "https://www.bbc.co.uk/iplayer"
SEARCH = "https://www.bbc.co.uk/iplayer/search?q="
KEYS = {  # key name -> (windowsVirtualKeyCode, code, text)
    "Enter": (13, "Enter", "\r"), "Backspace": (8, "Backspace", None), "Tab": (9, "Tab", None),
    "Escape": (27, "Escape", None), "Delete": (46, "Delete", None), "Home": (36, "Home", None),
    "End": (35, "End", None), "PageUp": (33, "PageUp", None), "PageDown": (34, "PageDown", None),
    "ArrowLeft": (37, "ArrowLeft", None), "ArrowUp": (38, "ArrowUp", None),
    "ArrowRight": (39, "ArrowRight", None), "ArrowDown": (40, "ArrowDown", None),
    " ": (32, "Space", " "),
}

# Runs in the page to describe what's being looked at.
PAGE_INFO_JS = r"""(() => {
  const q = (s) => document.querySelector(s);
  const meta = (p) => (q(`meta[property="${p}"]`) || {}).content || "";
  let series = null;
  for (const a of document.querySelectorAll('a[href*="/iplayer/episodes/"]')) {
    const m = a.getAttribute("href").match(/\/iplayer\/episodes\/([a-z0-9]{8})/);
    if (m) { series = m[1]; break; }
  }
  return { url: location.href, title: document.title || "", og_title: meta("og:title"),
           h1: ((q("h1") || {}).textContent || "").trim().slice(0, 200), episodes_pid: series };
})()"""


def derive_context(info: dict) -> dict:
    """Turn raw page info into what the Add buttons need. Pure function (unit-tested)."""
    url = info.get("url") or ""
    ep = re.search(r"bbc\.co\.uk/iplayer/episode/([a-z0-9]{8})", url)
    eps = re.search(r"bbc\.co\.uk/iplayer/episodes/([a-z0-9]{8})", url)
    sounds = re.search(r"bbc\.co\.uk/(?:sounds/play|programmes)/([a-z0-9]{8})", url)
    raw_title = info.get("og_title") or info.get("title") or ""
    title = re.sub(r"\s*[-|–]\s*BBC (?:iPlayer|Sounds)\s*$", "", raw_title).strip()
    h1 = (info.get("h1") or "").strip()
    if h1 and (eps or not title or title.lower().startswith(h1.lower())):
        name = h1
    else:
        name = re.split(r"\s+[-–]\s+|:\s+", title)[0].strip() if title else ""
    series_pid = eps.group(1) if eps else info.get("episodes_pid")
    episode_pid = ep.group(1) if ep else (sounds.group(1) if sounds else None)
    if ep:
        kind = "episode"
    elif eps:
        kind = "programme"
    elif sounds:
        kind = "sounds"
    else:
        kind = "other"
    return {"url": url, "kind": kind, "title": title or name, "programme_name": name,
            "episode_pid": episode_pid, "series_pid": series_pid,
            "episode_target": (f"https://www.bbc.co.uk/iplayer/episode/{ep.group(1)}" if ep
                               else url if sounds else None),
            "type": "radio" if sounds else "tv",
            "can_add_programme": bool(episode_pid),
            "can_add_series": bool(series_pid or episode_pid) and bool(name)}


def normalise_address(text: str) -> str:
    t = text.strip()
    if not t:
        return HOME
    if t.startswith("/"):
        return "https://www.bbc.co.uk" + t
    if re.match(r"^https?://", t, re.I):
        return t
    if " " not in t and "." in t:
        return "https://" + t
    return SEARCH + urllib.request.quote(t)


class CDP:
    """Minimal Chrome DevTools Protocol client over one page websocket."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True,
                                              enable_multithread=True)
        self.ws.settimeout(None)
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}
        self._send_lock = threading.Lock()
        self.handlers: dict[str, callable] = {}
        self.closed = False
        threading.Thread(target=self._reader, daemon=True, name="cdp-reader").start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "id" in msg:
                    slot = self._pending.pop(msg["id"], None)
                    if slot:
                        slot[1] = msg
                        slot[0].set()
                else:
                    h = self.handlers.get(msg.get("method"))
                    if h:
                        try:
                            h(msg.get("params", {}))
                        except Exception:  # noqa: BLE001
                            log.exception("CDP handler %s failed", msg.get("method"))
        except Exception:  # noqa: BLE001 - socket closed
            pass
        self.closed = True
        for slot in list(self._pending.values()):
            slot[0].set()

    def send(self, method: str, params: dict | None = None, wait: bool = True, timeout: float = 20):
        if self.closed:
            raise ConnectionError("browser connection closed")
        i = next(self._ids)
        slot = [threading.Event(), None]
        if wait:
            self._pending[i] = slot
        with self._send_lock:
            self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        if not wait:
            return None
        if not slot[0].wait(timeout):
            self._pending.pop(i, None)
            raise TimeoutError(f"{method} timed out")
        msg = slot[1]
        if msg is None:
            raise ConnectionError("browser connection closed")
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", str(msg["error"])))
        return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class RemoteBrowser:
    IDLE_STOP = 600          # stop Chromium after 10 minutes with nobody watching
    DEBUG_PORT = 0           # 0 = pick a free port each start

    def __init__(self, cfg, vpn, home: str = HOME, chromium: str | None = None,
                 use_proxy: bool = True, extra_args: list[str] | None = None):
        self.cfg, self.vpn, self.home = cfg, vpn, home
        self.extra_args = extra_args or []
        self.chromium = chromium or self._find_chromium()
        self.use_proxy = use_proxy
        self.proc: subprocess.Popen | None = None
        self.cdp: CDP | None = None
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.frame: bytes | None = None
        self.seq = 0
        self.size = (1280, 800)
        self.url = ""
        self.title = ""
        self.loading = False
        self.error: str | None = None
        self.last_active = 0.0
        threading.Thread(target=self._reaper, daemon=True, name="browser-reaper").start()

    @staticmethod
    def _find_chromium():
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
            p = shutil.which(name)
            if p:
                return p
        return "chromium"

    # --- lifecycle --------------------------------------------------------
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and self.cdp and not self.cdp.closed)

    def touch(self):
        self.last_active = time.time()

    def ensure(self, w: int | None = None, h: int | None = None) -> str | None:
        """Start the browser if needed. Returns an error message or None."""
        self.touch()
        with self.lock:
            if self.alive():
                if w and h:
                    self.resize(w, h)   # another device may have opened the tab at a different size
                return None
            if w and h:
                self.size = self._clamp(w, h)
            if self.use_proxy and not self.vpn.running():
                self.error = "The UK VPN isn't running — set it up on the VPN tab first"
                return self.error
            try:
                self._start()
                self.error = None
            except Exception as exc:  # noqa: BLE001
                log.exception("browser start failed")
                self.error = f"Couldn't start the browser: {exc}"
                self.stop()
            return self.error

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _kill_stale(profile) -> int:
        """Kill any Chromium left over from an earlier run using our profile; otherwise a new
        one would just hand over to it (single-instance lock) and we'd drive the wrong browser."""
        marker = f"--user-data-dir={profile}".encode()
        killed = 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    if marker in fh.read():
                        os.kill(int(pid), signal.SIGKILL)
                        killed += 1
            except OSError:
                continue
        if killed:
            time.sleep(0.5)
            for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                try:
                    os.unlink(profile / lock)
                except OSError:
                    pass
        return killed

    def _start(self):
        self.stop()
        w, h = self.size
        profile = self.cfg.data_dir / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        if self._kill_stale(profile):
            log.info("cleared a leftover browser process")
        port = self.DEBUG_PORT or self._free_port()
        args = [self.chromium, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                "--mute-audio", "--hide-scrollbars", "--lang=en-GB",
                "--disable-features=Translate,MediaRouter,OptimizationHints",
                "--autoplay-policy=document-user-activation-required",
                "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}", f"--window-size={w},{h}"]
        if self.use_proxy:
            # every request the browser makes goes through the UK tunnel
            args.append(f"--proxy-server={self.cfg.proxy_url}")
        args += self.extra_args
        args.append("about:blank")
        log.info("starting browser (%dx%d)%s", w, h, " via UK VPN" if self.use_proxy else "")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        deadline = time.time() + 25
        while time.time() < deadline and ws_url is None:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Chromium exited with code {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
            except OSError:
                time.sleep(0.3)
        if not ws_url:
            raise RuntimeError("Chromium didn't open its control port")
        self.cdp = CDP(ws_url)
        self.cdp.handlers.update({
            "Page.screencastFrame": self._on_frame,
            "Page.frameNavigated": self._on_navigated,
            "Page.frameStartedLoading": lambda p: setattr(self, "loading", True),
            "Page.loadEventFired": self._on_load,
        })
        self.cdp.send("Page.enable")
        self._apply_size()
        self.navigate(self.home)

    def stop(self):
        with self.lock:
            if self.cdp:
                self.cdp.close()
                self.cdp = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def _reaper(self):
        while True:
            time.sleep(30)
            if self.proc and time.time() - self.last_active > self.IDLE_STOP:
                log.info("stopping idle browser")
                self.stop()

    # --- events -----------------------------------------------------------
    def _on_frame(self, p):
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, wait=False)
        except Exception:  # noqa: BLE001
            pass
        with self.cond:
            self.frame = base64.b64decode(p["data"])
            self.seq += 1
            self.cond.notify_all()

    def _on_navigated(self, p):
        frame = p.get("frame", {})
        if not frame.get("parentId"):
            self.url = frame.get("url", self.url)

    def _on_load(self, _p):
        self.loading = False
        threading.Thread(target=self._refresh_title, daemon=True).start()

    def _refresh_title(self):
        try:
            r = self.cdp.send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
            self.title = r.get("result", {}).get("value") or ""
        except Exception:  # noqa: BLE001
            pass

    # --- screen -----------------------------------------------------------
    @staticmethod
    def _clamp(w, h):
        return max(320, min(int(w), 1920)), max(320, min(int(h), 1400))

    def _apply_size(self):
        w, h = self.size
        self.cdp.send("Emulation.setDeviceMetricsOverride",
                      {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 700})
        try:
            self.cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001
            pass
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70,
                                               "maxWidth": w, "maxHeight": h, "everyNthFrame": 1})

    def resize(self, w, h):
        self.touch()
        with self.lock:
            new = self._clamp(w, h)
            if new != self.size:
                self.size = new
                if self.alive():
                    self._apply_size()
        return self.size

    def get_frame(self, after: int, timeout: float = 15):
        self.touch()
        with self.cond:
            self.cond.wait_for(lambda: self.seq > after, timeout)
            if self.seq > after and self.frame is not None:
                return self.seq, self.frame
        return None

    # --- navigation & input ---------------------------------------------
    def _send(self, method, params=None, **kw):
        self.touch()
        if not self.alive():
            raise ConnectionError("The browser isn't running")
        return self.cdp.send(method, params, **kw)

    def navigate(self, address: str):
        url = normalise_address(address)
        self.loading = True
        self._send("Page.navigate", {"url": url})
        return url

    def history(self, step: int):
        h = self._send("Page.getNavigationHistory")
        i = h["currentIndex"] + step
        if 0 <= i < len(h["entries"]):
            self._send("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})

    def reload(self):
        self._send("Page.reload")

    def mouse(self, kind: str, x: float, y: float, button: str = "left", clicks: int = 1):
        types = {"down": ["mousePressed"], "up": ["mouseReleased"], "move": ["mouseMoved"],
                 "click": ["mousePressed", "mouseReleased"]}[kind]
        for t in types:
            self._send("Input.dispatchMouseEvent",
                       {"type": t, "x": x, "y": y, "button": "none" if t == "mouseMoved" else button,
                        "clickCount": 0 if t == "mouseMoved" else clicks})

    def wheel(self, x: float, y: float, dx: float, dy: float):
        self._send("Input.dispatchMouseEvent",
                   {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})

    def key(self, key: str):
        if key not in KEYS:
            raise ValueError("unsupported key")
        vk, code, text = KEYS[key]
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        if text:
            down["text"] = text
        self._send("Input.dispatchKeyEvent", down)
        self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                              "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})

    def text(self, s: str):
        self._send("Input.insertText", {"text": s[:2000]})

    def page_info(self) -> dict:
        r = self._send("Runtime.evaluate", {"expression": PAGE_INFO_JS, "returnByValue": True})
        return r.get("result", {}).get("value") or {"url": self.url}

    def state(self) -> dict:
        return {"running": self.alive(), "url": self.url, "title": self.title,
                "loading": self.loading, "error": self.error, "seq": self.seq,
                "size": list(self.size), "proxied": self.use_proxy}
