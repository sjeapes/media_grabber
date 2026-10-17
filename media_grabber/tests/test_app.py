import json
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "rootfs" / "app"))

from mediagrabber import config  # noqa: E402
from mediagrabber.vpn import ConfigError, parse_wg, render_wireproxy  # noqa: E402
from mediagrabber.web import Services, create_app  # noqa: E402

STUBS = HERE / "stubs"
PROTON = """[Interface]
# Key for HA
PrivateKey = aPrivKeyBase64aaaaaaaaaaaaaaaaaaaaaaaaaaaaa=
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
# UK#123
PublicKey = bPubKeyBase64bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb=
AllowedIPs = 0.0.0.0/0
Endpoint = 185.1.2.3:51820
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    nas.mkdir()
    data = tmp_path / "data"
    mounts = tmp_path / "mounts"
    mounts.write_text(f"overlay / overlay rw 0 0\n//192.168.1.50/media {nas} cifs rw 0 0\n")
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"nas_path": str(nas), "low_space_gb": 1}))
    monkeypatch.setenv("MG_DATA_DIR", str(data))
    monkeypatch.setenv("MG_ADDON_CONFIG", str(tmp_path / "addon_config"))
    monkeypatch.setenv("MG_GET_IPLAYER", str(STUBS / "get_iplayer"))
    monkeypatch.setenv("MG_YT_DLP", str(STUBS / "yt-dlp"))
    monkeypatch.setenv("MG_WIREPROXY", str(STUBS / "wireproxy"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    cfg = config.load()
    svc = Services(cfg, mounts_file=str(mounts))
    svc.jobs.start_workers(1)
    app = create_app(svc)
    app.testing = True
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "172.30.32.2"
    yield {"svc": svc, "c": client, "nas": nas, "mounts": mounts, "cfg": cfg}
    svc.vpn.shutdown()


def uk(svc, monkeypatch, country="GB"):
    monkeypatch.setattr(svc.vpn, "_fetch_via_proxy",
                        lambda url, timeout=15: {"country": country, "city": "London",
                                                 "org": "Proton AG", "ip": "1.2.3.4"})


def wait_job(c, jid, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        j = next(x for x in c.get("/api/jobs").json if x["id"] == jid)
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# --- VPN config ---------------------------------------------------------
def test_parse_and_render_proton_config():
    p = parse_wg(PROTON)
    out = render_wireproxy(p, "127.0.0.1:18118")
    assert "PersistentKeepalive = 25" in out
    assert "[http]\nBindAddress = 127.0.0.1:18118" in out
    assert "UK#123" not in out


def test_parse_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_wg("hello")


def test_only_ingress_clients(env):
    c = env["c"]
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.1.20"}).status_code == 403


# --- NAS ------------------------------------------------------------------
def test_nas_healthy_when_network_mounted(env):
    s = env["c"].get("/api/status").json["nas"]
    assert s["healthy"] and s["network_mount"] and s["fstype"] == "cifs"
    assert s["probe"]["ok"] and s["disk"]["total_gb"] > 0
    assert not list(env["nas"].iterdir()), "probe file must be cleaned up"


def test_nas_unmounted_blocks_downloads(env, monkeypatch):
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    s = env["c"].post("/api/nas/check").json
    assert not s["healthy"]
    assert any("not mounted" in p for p in s["problems"])
    r = env["c"].post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    j = wait_job(env["c"], r.json["id"])
    assert j["status"] == "failed" and "NAS not ready" in j["error"]


def test_nas_missing(env):
    env["svc"].cfg.options["nas_path"] = "/does/not/exist"
    s = env["c"].get("/api/status").json["nas"]
    assert not s["healthy"] and "Settings" in s["problems"][0]


# --- iPlayer ----------------------------------------------------------
def test_iplayer_blocked_without_vpn(env):
    r = env["c"].get("/api/iplayer/search?q=bluey")
    assert r.status_code == 409 and "VPN" in r.json["error"]


def test_iplayer_blocked_when_exit_not_uk(env, monkeypatch):
    svc = env["svc"]
    uk(svc, monkeypatch, "US")
    assert env["c"].post("/api/vpn", json={"config": PROTON}).status_code == 200
    time.sleep(0.3)
    svc.vpn.check_exit(force=True)
    r = env["c"].get("/api/iplayer/search?q=bluey")
    assert r.status_code == 409 and "not the UK" in r.json["error"]


def test_iplayer_search_and_download_via_vpn(env, monkeypatch):
    svc, c = env["svc"], env["c"]
    uk(svc, monkeypatch)
    c.post("/api/vpn", json={"config": PROTON})
    time.sleep(0.3)
    svc.vpn.check_exit(force=True)
    st = c.get("/api/vpn").json
    assert st["uk"] and st["endpoint"] == "185.1.2.3:51820"
    assert "PrivateKey" not in json.dumps(st)
    assert oct(svc.cfg.wg_conf.stat().st_mode)[-3:] == "600"

    rows = c.get("/api/iplayer/search?q=bluey").json
    assert [r["pid"] for r in rows] == ["m001abcd", "m002efgh"]
    assert rows[1]["desc"] == "Bandit | Chilli do chores." and rows[0]["minutes"] == 7
    args = (STUBS / "get_iplayer.last").read_text().split("\n")
    assert args[args.index("--proxy") + 1] == "http://127.0.0.1:18118"

    r = c.post("/api/iplayer/download", json={"target": "m001abcd", "title": "Bluey — Stickbird"})
    assert r.status_code == 201
    assert c.post("/api/iplayer/download", json={"target": "m001abcd"}).status_code in (201, 409)
    j = wait_job(c, r.json["id"])
    assert j["status"] == "done" and j["progress"] == 100
    assert j["files"] and j["files"][0].endswith(".mp4'")
    args = (STUBS / "get_iplayer.last").read_text().split("\n")
    assert args[args.index("--output") + 1] == str(env["nas"] / "iPlayer")
    assert "--proxy" in args
    # yt-dlp must NOT use the tunnel by default
    r = c.post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    wait_job(c, r.json["id"])
    assert "--proxy" not in (STUBS / "yt-dlp.last").read_text()


def test_iplayer_failed_record_is_failure(env, monkeypatch):
    svc, c = env["svc"], env["c"]
    svc.cfg.options["iplayer_requires_uk_vpn"] = False
    r = c.post("/api/iplayer/download", json={"target": "https://www.bbc.co.uk/iplayer/episode/FAILME"})
    j = wait_job(c, r.json["id"])
    assert j["status"] == "failed" and "Failed to record" in j["error"]


def test_bad_iplayer_target(env):
    r = env["c"].post("/api/iplayer/download", json={"target": "rm -rf /"})
    assert r.status_code == 400


# --- links ------------------------------------------------------------
def test_youtube_download_progress_and_paths(env):
    c = env["c"]
    r = c.post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc", "audio_only": True})
    j = wait_job(c, r.json["id"])
    assert j["status"] == "done"
    assert j["files"] == ["/nas/YouTube/Chan/Video [abc].mp4"]
    args = (STUBS / "yt-dlp.last").read_text().split("\n")
    assert f"home:{env['nas'] / 'YouTube'}" in args and "-x" in args
    assert args[-2:] == ["--", "https://www.youtube.com/watch?v=abc"]


def test_drm_sites_refused(env):
    r = env["c"].post("/api/link/download", json={"url": "https://www.itv.com/watch/x"})
    assert r.status_code == 400 and "DRM" in r.json["error"]


def test_progress_parser(env):
    q = env["svc"].jobs
    j = {"kind": "link", "progress": 0, "speed": None, "eta": None, "log": [], "files": []}
    q._parse(j, "MGPROGRESS  50.2%    2.10MiB/s ETA 00:12")
    assert j["progress"] == 50.2 and j["speed"] == "2.10MiB/s" and j["eta"] == "00:12"
    q._parse(j, " 45.5% of ~1.20 GiB @  8.4 Mb/s ETA: 00:05:00 (hlsvideo/x)")
    assert j["progress"] == 45.5 and j["eta"] == "00:05:00"


# --- PVR & persistence ----------------------------------------------------
def test_pvr_add_list_delete_run(env, monkeypatch):
    svc, c = env["svc"], env["c"]
    assert c.post("/api/pvr", json={"query": "ab"}).status_code == 400
    item = c.post("/api/pvr", json={"query": "Hey Duggee", "name": "Hey Duggee"}).json
    assert [i["id"] for i in c.get("/api/pvr").json] == [item["id"]]
    svc.cfg.options["iplayer_requires_uk_vpn"] = False
    assert c.post("/api/pvr/run").json["queued"] == 1
    cmd = next(j for j in svc.jobs.jobs.values() if j["meta"].get("pvr") == item["id"])["cmd"]
    assert cmd[-2:] == ["--get", "Hey Duggee"]
    assert c.delete(f"/api/pvr/{item['id']}").json["ok"]


def test_running_jobs_marked_interrupted_on_restart(env):
    cfg = env["cfg"]
    (cfg.data_dir / "jobs.json").write_text(json.dumps([{
        "id": "x1", "kind": "link", "title": "t", "cmd": ["true"], "meta": {}, "status": "running",
        "progress": 40, "speed": None, "eta": None, "error": None, "files": [], "log": [],
        "created": 0, "started": 0, "finished": None}]))
    svc2 = Services(cfg, mounts_file=str(env["mounts"]))
    assert svc2.jobs.jobs["x1"]["status"] == "failed"


def test_index_page_served(env):
    r = env["c"].get("/")
    assert r.status_code == 200 and b"Media Grabber" in r.data
    assert env["c"].get("/static/app.js").status_code == 200
