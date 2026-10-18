"""Split-tunnel UK VPN.

wireproxy runs WireGuard in userspace and exposes it as an HTTP proxy bound to
127.0.0.1 inside this container only. get_iplayer (and optionally yt-dlp) is
pointed at it; nothing else on the host or in Home Assistant is routed through
the tunnel, and no extra container privileges are needed.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

INTERFACE_KEYS = {"privatekey", "address", "dns", "mtu"}
log = logging.getLogger("mediagrabber.vpn")
PEER_KEYS = {"publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive"}


class ConfigError(ValueError):
    pass


def parse_wg(text: str) -> dict:
    """Parse a wg-quick style config into {'interface': {...}, 'peers': [{...}]}."""
    section, iface, peers = None, {}, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            if name == "interface":
                section = iface
            elif name == "peer":
                section = {}
                peers.append(section)
            else:
                section = None
            continue
        if "=" not in line or section is None:
            continue
        k, v = line.split("=", 1)
        section[k.strip().lower()] = v.strip()
    if "privatekey" not in iface or "address" not in iface:
        raise ConfigError("Missing [Interface] PrivateKey or Address")
    if not peers or any("publickey" not in p or "endpoint" not in p for p in peers):
        raise ConfigError("Missing [Peer] PublicKey or Endpoint")
    return {"interface": iface, "peers": peers}


def render_wireproxy(parsed: dict, bind: str) -> str:
    names = {"privatekey": "PrivateKey", "address": "Address", "dns": "DNS", "mtu": "MTU",
             "publickey": "PublicKey", "presharedkey": "PresharedKey", "endpoint": "Endpoint",
             "allowedips": "AllowedIPs", "persistentkeepalive": "PersistentKeepalive"}
    out = ["[Interface]"]
    for k, v in parsed["interface"].items():
        if k in INTERFACE_KEYS:
            out.append(f"{names[k]} = {v}")
    for p in parsed["peers"]:
        out += ["", "[Peer]"]
        p = dict(p)
        p.setdefault("allowedips", "0.0.0.0/0")
        p.setdefault("persistentkeepalive", "25")
        for k, v in p.items():
            if k in PEER_KEYS:
                out.append(f"{names[k]} = {v}")
    out += ["", "[http]", f"BindAddress = {bind}", ""]
    return "\n".join(out)


class Vpn:
    CHECK_EVERY = 600

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.log = collections.deque(maxlen=40)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._exit = {"country": None, "city": None, "org": None, "ip": None,
                      "at": None, "error": None}
        self._restarts = 0

    # --- config -----------------------------------------------------------
    @property
    def runtime_conf(self):
        return self.cfg.data_dir / "wireproxy.conf"

    def _source_conf(self):
        """UI-pasted config wins; otherwise a wireguard.conf dropped in the add-on config folder."""
        if self.cfg.wg_conf.exists():
            return self.cfg.wg_conf
        alt = self.cfg.addon_config_dir / "wireguard.conf"
        return alt if alt.exists() else None

    @property
    def configured(self) -> bool:
        return self._source_conf() is not None

    def summary(self) -> dict:
        src = self._source_conf()
        if not src:
            return {"configured": False}
        try:
            p = parse_wg(src.read_text())
            peer = p["peers"][0]
            return {"configured": True, "endpoint": peer.get("endpoint"),
                    "address": p["interface"].get("address"),
                    "source": "web UI" if src == self.cfg.wg_conf else str(src)}
        except (OSError, ConfigError) as exc:
            return {"configured": True, "error": str(exc)}

    def save_config(self, text: str) -> dict:
        parse_wg(text)  # validate before saving
        self.cfg.wg_conf.write_text(text.strip() + "\n")
        os.chmod(self.cfg.wg_conf, 0o600)
        self.restart()
        return self.summary()

    def delete_config(self):
        if self.cfg.wg_conf.exists():
            self.cfg.wg_conf.unlink()
        self.stop_proc()
        self._exit = {k: None for k in self._exit}

    # --- process ----------------------------------------------------------
    def _start_proc(self):
        src = self._source_conf()
        if not src:
            return
        try:
            conf = render_wireproxy(parse_wg(src.read_text()), f"127.0.0.1:{self.cfg.proxy_port}")
        except ConfigError as exc:
            self.log.append(f"config error: {exc}")
            return
        self.runtime_conf.write_text(conf)
        os.chmod(self.runtime_conf, 0o600)
        self.proc = subprocess.Popen([self.cfg.wireproxy, "-c", str(self.runtime_conf)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.log.append("wireproxy started")

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if line and "PrivateKey" not in line:
                self.log.append(line[-300:])
                low = line.lower()
                if any(w in low for w in ("error", "fail", "timeout", "timed out", "unreachable")):
                    log.warning("wireproxy: %s", line[-300:])

    def stop_proc(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def restart(self):
        self.stop_proc()
        with self._lock:
            self._start_proc()
        self._exit["at"] = None  # force a fresh exit check
        threading.Thread(target=lambda: (time.sleep(3), self.check_exit(force=True)), daemon=True).start()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def supervise(self):
        """Keep wireproxy alive; re-check the exit country periodically."""
        backoff = 5
        with self._lock:
            self._start_proc()
        while not self._stop.wait(5):
            if self.configured and not self.running():
                self.log.append(f"wireproxy not running; restarting in {backoff}s")
                if self._stop.wait(backoff):
                    break
                with self._lock:
                    self._start_proc()
                self._restarts += 1
                backoff = min(backoff * 2, 300)
            elif self.running():
                backoff = 5
                self.check_exit()

    # --- exit check -----------------------------------------------------
    def _fetch_via_proxy(self, url, timeout=15):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": self.cfg.proxy_url, "https": self.cfg.proxy_url}))
        req = urllib.request.Request(url, headers={"User-Agent": "media-grabber/1"})
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def check_exit(self, force=False) -> dict:
        if not force and self._exit["at"] and time.time() - self._exit["at"] < self.CHECK_EVERY:
            return self._exit
        if not self.running():
            self._exit = {**{k: None for k in self._exit}, "at": time.time(),
                          "error": "tunnel not running" if self.configured else "not configured"}
            return self._exit
        try:
            d = self._fetch_via_proxy("https://ipinfo.io/json")
            res = {"country": d.get("country"), "city": d.get("city"), "org": d.get("org"),
                   "ip": d.get("ip"), "error": None}
        except Exception as exc:  # noqa: BLE001 - try a second provider
            try:
                d = self._fetch_via_proxy("http://ip-api.com/json")
                res = {"country": d.get("countryCode"), "city": d.get("city"), "org": d.get("isp"),
                       "ip": d.get("query"), "error": None}
            except Exception as exc2:  # noqa: BLE001
                res = {"country": None, "city": None, "org": None, "ip": None,
                       "error": f"exit check failed: {exc2 or exc}"}
        self._exit = {**res, "at": time.time()}
        return self._exit

    def uk_ok(self) -> bool:
        e = self.check_exit()
        return self.running() and e.get("country") == "GB"

    def status(self) -> dict:
        return {**self.summary(), "running": self.running(), "exit": dict(self._exit),
                "uk": self.running() and self._exit.get("country") == "GB",
                "restarts": self._restarts, "log": list(self.log)[-12:]}

    def shutdown(self):
        self._stop.set()
        self.stop_proc()
