import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "rootfs" / "app"))

from mediagrabber.browser import RemoteBrowser, derive_context, normalise_address  # noqa: E402
from test_app import env, wait_job  # noqa: E402,F401  (fixture reuse)

FAKE = HERE / "fake_bbc"
CHROME = next((p for p in [shutil.which("chromium"), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
               if p and os.path.exists(p)), None)


# --- pure helpers -----------------------------------------------------------
def test_derive_context_episode_page():
    c = derive_context({"url": "https://www.bbc.co.uk/iplayer/episode/m001abcd/the-celebrity-traitors-series-2-episode-1",
                        "title": "The Celebrity Traitors - Series 2: Episode 1 - BBC iPlayer",
                        "og_title": "", "h1": "The Celebrity Traitors", "episodes_pid": "m0012345"})
    assert c["kind"] == "episode" and c["episode_pid"] == "m001abcd" and c["series_pid"] == "m0012345"
    assert c["programme_name"] == "The Celebrity Traitors"
    assert c["title"] == "The Celebrity Traitors - Series 2: Episode 1"
    assert c["episode_target"] == "https://www.bbc.co.uk/iplayer/episode/m001abcd"
    assert c["can_add_programme"] and c["can_add_series"]


def test_derive_context_without_h1_uses_title():
    c = derive_context({"url": "https://www.bbc.co.uk/iplayer/episode/p0abc123/x",
                        "title": "Bluey: Series 3: 1. Stickbird - BBC iPlayer", "h1": ""})
    assert c["programme_name"] == "Bluey" and c["series_pid"] is None and c["can_add_series"]


def test_derive_context_programme_and_other_pages():
    c = derive_context({"url": "https://www.bbc.co.uk/iplayer/episodes/m0012345/the-celebrity-traitors",
                        "title": "The Celebrity Traitors - BBC iPlayer", "h1": "The Celebrity Traitors"})
    assert c["kind"] == "programme" and c["series_pid"] == "m0012345"
    assert not c["can_add_programme"] and c["can_add_series"]
    c = derive_context({"url": "https://www.bbc.co.uk/iplayer", "title": "BBC iPlayer - Home", "h1": "Home"})
    assert c["kind"] == "other" and not c["can_add_programme"] and not c["can_add_series"]
    c = derive_context({"url": "https://www.bbc.co.uk/sounds/play/m00abcde", "title": "Desert Island Discs - BBC Sounds"})
    assert c["kind"] == "sounds" and c["type"] == "radio" and c["can_add_programme"]


def test_normalise_address():
    assert normalise_address("") == "https://www.bbc.co.uk/iplayer"
    assert normalise_address("bbc.co.uk/iplayer/categories") == "https://bbc.co.uk/iplayer/categories"
    assert normalise_address("/iplayer/a-z") == "https://www.bbc.co.uk/iplayer/a-z"
    assert normalise_address("hey duggee") == "https://www.bbc.co.uk/iplayer/search?q=hey%20duggee"
    assert normalise_address("http://x.test/y") == "http://x.test/y"


def test_browser_refuses_without_vpn(env):
    c = env["c"]
    r = c.post("/api/browser/start", json={"w": 800, "h": 600})
    assert r.status_code == 409 and "VPN" in r.json["error"]


# --- real Chromium against fake BBC pages ---------------------------------
class FakeBBC(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split("?")[0]
        for prefix in ("/iplayer/episode/m001abcd", "/iplayer/episodes/m0012345"):
            if p.startswith(prefix):
                return str(FAKE / prefix.lstrip("/") / "index.html")
        return str(FAKE / "iplayer" / "index.html")

    def log_message(self, *a):
        pass


@pytest.fixture
def live(env, tmp_path):
    if not CHROME:
        pytest.skip("no Chromium available")
    # bbc.co.uk is HSTS-preloaded, so the fake site must be HTTPS (self-signed, errors ignored)
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=www.bbc.co.uk", "-keyout", key, "-out", crt],
                   check=True, capture_output=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBBC)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    svc = env["svc"]
    svc.browser = RemoteBrowser(svc.cfg, svc.vpn, home="https://www.bbc.co.uk/iplayer", chromium=CHROME,
                                use_proxy=False,
                                extra_args=[f"--host-resolver-rules=MAP www.bbc.co.uk:443 127.0.0.1:{port}",
                                            "--ignore-certificate-errors", "--no-proxy-server"])
    yield env
    svc.browser.stop()
    srv.shutdown()


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_browse_click_add_programme_and_series(live):
    c, svc = live["c"], live["svc"]
    r = c.post("/api/browser/start", json={"w": 800, "h": 600})
    assert r.status_code == 200, r.json
    wait_for(lambda: "/iplayer" in svc.browser.url and not svc.browser.loading)
    # frames arrive as JPEG
    f = c.get("/api/browser/frame?after=0")
    assert f.status_code == 200 and f.data[:2] == b"\xff\xd8" and int(f.headers["X-Seq"]) >= 1
    ctx = c.get("/api/browser/context").json
    assert ctx["kind"] == "other" and not ctx["can_add_programme"]
    assert c.post("/api/browser/add", json={"mode": "programme"}).status_code == 400
    # click the programme tile (left 20..320, top 100..160 in page coordinates)
    assert c.post("/api/browser/input", json={"type": "click", "x": 100, "y": 130}).status_code == 200
    wait_for(lambda: "/iplayer/episode/m001abcd" in svc.browser.url)
    wait_for(lambda: c.get("/api/browser/context").json.get("series_pid") == "m0012345")
    ctx = c.get("/api/browser/context").json
    assert ctx["programme_name"] == "The Celebrity Traitors" and ctx["can_add_programme"]

    svc.cfg.options["iplayer_requires_uk_vpn"] = False
    r = c.post("/api/browser/add", json={"mode": "programme"})
    assert r.status_code == 201 and "Series 2: Episode 1" in r.json["message"]
    assert c.post("/api/browser/add", json={"mode": "programme"}).status_code == 409  # already queued
    r = c.post("/api/browser/add", json={"mode": "series", "name": "The Celebrity Traitors"})
    assert r.status_code == 201, r.json
    assert "all available episodes queued" in r.json["message"]
    assert "new episodes will be recorded" in r.json["message"]
    specs = [j["spec"] for j in svc.jobs.jobs.values()]
    assert {"type": "iplayer", "target": "https://www.bbc.co.uk/iplayer/episode/m001abcd",
            "series": False, "subtitles": False} in specs
    assert {"type": "iplayer", "target": "m0012345", "series": True, "subtitles": False} in specs
    assert [p["query"] for p in svc.dl.pvr_list()] == ["The Celebrity Traitors"]
    r = c.post("/api/browser/add", json={"mode": "series", "name": "the celebrity traitors",
                                          "download_existing": False})
    assert "already recording" in r.json["message"] and len(svc.dl.pvr_list()) == 1

    # navigation: back, address bar, typing
    c.post("/api/browser/nav", json={"action": "back"})
    wait_for(lambda: svc.browser.url.rstrip("/").endswith("/iplayer"))
    c.post("/api/browser/input", json={"type": "click", "x": 100, "y": 215})
    c.post("/api/browser/input", json={"type": "text", "text": "bluey"})
    val = svc.browser.cdp.send("Runtime.evaluate", {"expression": "document.getElementById('q').value",
                                                    "returnByValue": True})["result"]["value"]
    assert val == "bluey"
    c.post("/api/browser/input", json={"type": "wheel", "x": 100, "y": 100, "dx": 0, "dy": 500})
    wait_for(lambda: svc.browser.cdp.send("Runtime.evaluate", {"expression": "scrollY", "returnByValue": True})
             ["result"]["value"] > 100)
    c.post("/api/browser/nav", json={"url": "/iplayer/episodes/m0012345"})
    wait_for(lambda: "/iplayer/episodes/m0012345" in svc.browser.url)
    assert c.post("/api/browser/resize", json={"w": 400, "h": 700}).json["size"] == [400, 700]
    st = c.get("/api/browser/state").json
    assert st["running"] and st["title"]
    svc.browser.stop()
    assert not svc.browser.alive()


def test_leftover_browser_is_replaced(live):
    svc = live["svc"]
    old = svc.browser
    assert old.ensure(640, 480) is None
    old_pid = old.proc.pid
    # simulate the app restarting while the old Chromium keeps running
    fresh = RemoteBrowser(svc.cfg, svc.vpn, home=old.home, chromium=old.chromium, use_proxy=False,
                          extra_args=old.extra_args)
    old.proc = None  # forget it without stopping it
    assert fresh.ensure(640, 480) is None and fresh.proc.pid != old_pid
    wait_for(lambda: not os.path.exists(f"/proc/{old_pid}") or
             open(f"/proc/{old_pid}/stat").read().split()[2] == "Z")
    wait_for(lambda: "/iplayer" in fresh.url)
    fresh.stop()
