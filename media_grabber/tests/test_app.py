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
    media = tmp_path / "media"
    nas = media / "Synology"
    nas.mkdir(parents=True)
    (tmp_path / "share").mkdir()
    data = tmp_path / "data"
    mounts = tmp_path / "mounts"
    mounts.write_text(f"overlay / overlay rw 0 0\n/dev/sda8 {media} ext4 rw 0 0\n"
                      f"//192.168.1.50/media {nas} cifs rw 0 0\n")
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"low_space_gb": 1, "local_min_free_gb": 1,
                                                   "nas_path": "/media/nas"}))
    monkeypatch.setenv("MG_DATA_DIR", str(data))
    monkeypatch.setenv("MG_MEDIA_ROOT", str(media))
    monkeypatch.setenv("MG_SHARE_ROOT", str(tmp_path / "share"))
    monkeypatch.setenv("MG_ADDON_CONFIG", str(tmp_path / "addon_config"))
    monkeypatch.setenv("MG_GET_IPLAYER", str(STUBS / "get_iplayer"))
    monkeypatch.setenv("MG_YT_DLP", str(STUBS / "yt-dlp"))
    monkeypatch.setenv("MG_WIREPROXY", str(STUBS / "wireproxy"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    cfg = config.load()
    svc = Services(cfg, mounts_file=str(mounts))
    svc.jobs.retry_delays = (0, 0)
    svc.jobs.start_workers(1)
    app = create_app(svc)
    app.testing = True
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "172.30.32.2"
    yield {"svc": svc, "c": client, "nas": nas, "mounts": mounts, "cfg": cfg, "tmp": tmp_path,
           "media": media}
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
def test_single_nas_is_detected_automatically(env):
    s = env["c"].get("/api/status").json["nas"]
    assert s["healthy"] and s["network_mount"] and s["fstype"] == "cifs"
    assert s["choice"] == "auto" and s["name"] == "Synology" and s["path"] == str(env["nas"])
    assert s["folders"]["iplayer"] == str(env["nas"] / "iPlayer")
    assert s["probe"]["ok"] and s["disk"]["total_gb"] > 0
    assert not list(env["nas"].iterdir()), "probe file must be cleaned up"


def test_nas_unmounted_blocks_downloads(env, monkeypatch):
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    s = env["c"].post("/api/nas/check").json
    assert not s["healthy"] and s["mode"] is None
    assert "No network storage found" in s["problems"][0] and "Home Assistant storage" in s["problems"][0]
    r = env["c"].post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    j = wait_job(env["c"], r.json["id"])
    assert j["status"] == "failed" and "Storage not ready" in j["error"]
    assert not (env["media"] / "MediaGrabber").exists(), "must never fall back to local disk"


def test_selected_nas_unmounted_does_not_fall_back(env):
    c = env["c"]
    assert c.post("/api/storage/select", json={"choice": str(env["nas"])}).json["healthy"]
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    s = c.get("/api/status").json["nas"]
    assert not s["healthy"] and "“Synology” isn't mounted" in s["problems"][0]


def test_multiple_nas_requires_choice_then_uses_it(env):
    c, media = env["c"], env["media"]
    (media / "QNAP").mkdir()
    (env["tmp"] / "share" / "backups").mkdir()
    env["mounts"].write_text(env["mounts"].read_text() +
                             f"10.0.0.9:/vol/tv {media / 'QNAP'} nfs4 rw 0 0\n"
                             f"//10.0.0.9/b {env['tmp'] / 'share' / 'backups'} cifs rw 0 0\n")
    env["svc"].cfg.options.pop("nas_path")
    s = c.get("/api/status").json["nas"]
    assert not s["healthy"] and s["needs_choice"] and s["candidate_count"] == 3
    opts = c.get("/api/storage/options").json
    assert [x["name"] for x in opts["candidates"]] == ["QNAP", "Synology", "backups"]
    assert opts["candidates"][2]["usage"] == "Share" and opts["candidates"][0]["free_gb"] is not None
    assert c.post("/api/storage/select", json={"choice": "/etc"}).status_code == 400
    s = c.post("/api/storage/select", json={"choice": str(media / "QNAP")}).json
    assert s["healthy"] and s["name"] == "QNAP" and s["fstype"] == "nfs4"
    r = c.post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    j = wait_job(c, r.json["id"])
    assert j["status"] == "done" and j["target"] == str(media / "QNAP")
    assert f"home:{media / 'QNAP' / 'YouTube'}" in (STUBS / "yt-dlp.last").read_text().split("\n")
    # choice survives a restart
    assert Services(env["cfg"], mounts_file=str(env["mounts"])).storage.status()["name"] == "QNAP"


def test_legacy_nas_path_is_preferred_when_present(env):
    media = env["media"]
    (media / "nas").mkdir()
    env["mounts"].write_text(env["mounts"].read_text() + f"//x/y {media / 'nas'} cifs rw 0 0\n")
    env["svc"].cfg.options["nas_path"] = str(media / "nas")
    assert env["c"].get("/api/status").json["nas"]["name"] == "nas"


def test_local_storage_option(env):
    c, media = env["c"], env["media"]
    s = c.post("/api/storage/select", json={"choice": "local"}).json
    assert s["healthy"] and s["mode"] == "local" and s["path"] == str(media / "MediaGrabber")
    assert s["warnings"] and (media / "MediaGrabber").is_dir()
    r = c.post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert wait_job(c, r.json["id"])["status"] == "done"
    env["svc"].cfg.options["local_min_free_gb"] = 10 ** 6
    s = c.get("/api/status").json["nas"]
    assert not s["healthy"] and "keeps working" in s["problems"][0]


def test_running_job_stopped_when_storage_goes(env):
    svc, c = env["svc"], env["c"]
    r = c.post("/api/link/download", json={"url": "https://www.youtube.com/watch?v=abc"})
    wait_job(c, r.json["id"])
    job = svc.jobs.add("link", "long", {"type": "link", "url": "https://x.test/v"})
    with svc.jobs.lock:
        job["status"], job["target"] = "running", str(env["nas"])
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    st = svc.storage.status(probe=False)
    still = {x["path"] for x in svc.storage.candidates()}
    assert not st["healthy"] and str(env["nas"]) not in still
    svc.jobs.cancel(job["id"], error="Stopped: " + st["problems"][0])
    assert job["status"] == "failed" and job["error"].startswith("Stopped: No network storage")


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
    assert j["attempt"] == 3 and len(j["past_errors"]) == 2
    recent = svc.jobs.recent()
    assert recent[0]["status"] == "failed" and recent[0]["attempts"] == 3


def test_transient_failure_retries_then_succeeds(env, tmp_path):
    svc, c = env["svc"], env["c"]
    svc.cfg.options["iplayer_requires_uk_vpn"] = False
    flag = tmp_path / "flaky"
    r = c.post("/api/iplayer/download",
               json={"target": f"https://www.bbc.co.uk/iplayer/episode/FLAKY{flag}"})
    j = wait_job(c, r.json["id"])
    assert j["status"] == "done" and j["attempt"] == 2
    assert "Failed to record" in j["past_errors"][0]
    assert any("retrying" in l for l in svc.jobs.jobs[j["id"]]["log"])


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
    job = next(j for j in svc.jobs.jobs.values() if j["meta"].get("pvr") == item["id"])
    svc.storage.resolve()
    assert svc.build_cmd(job["spec"])[-2:] == ["--get", "Hey Duggee"]
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


# --- disk usage scan ------------------------------------------------------
def test_usage_scan_tree_and_summary(env, monkeypatch):
    import mediagrabber.usage as usage
    monkeypatch.setattr(usage, "KEEP_BYTES", 1000)
    nas, c = env["nas"], env["c"]
    (nas / "Films" / "Big").mkdir(parents=True)
    (nas / "Films" / "Big" / "movie.mkv").write_bytes(b"x" * 50_000)
    (nas / "Films" / "tiny").mkdir()
    (nas / "Films" / "tiny" / "a.txt").write_bytes(b"x" * 10)
    (nas / "Photos").mkdir()
    (nas / "Photos" / "p.jpg").write_bytes(b"x" * 20_000)
    (nas / "#recycle").mkdir()
    (nas / "#recycle" / "old.iso").write_bytes(b"x" * 30_000)
    (nas / "readme.txt").write_bytes(b"x" * 5)
    (nas / "link").symlink_to(nas / "Films")
    # a different mount nested inside must be skipped when scanning /media
    targets = c.get("/api/usage").json["targets"]
    assert [t["name"] for t in targets] == ["Synology", "Home Assistant media folder"]
    assert c.post("/api/usage/scan", json={"path": "/etc"}).status_code == 400
    assert c.post("/api/usage/scan", json={"path": str(nas)}).status_code == 200
    for _ in range(50):
        u = c.get("/api/usage").json
        if not u["state"]["running"]:
            break
        time.sleep(0.1)
    assert u["root"]["s"] == 50_000 + 10 + 20_000 + 30_000 + 5  # symlink not followed
    kids = [k["name"] for k in u["root"]["children"]]
    assert kids == ["Films", "#recycle", "Photos"]
    assert u["root"]["own_files"] == 5
    assert u["recycle_bins"][0]["path"] == "#recycle"
    films = c.get("/api/usage/children?rel=Films").json
    assert [k["name"] for k in films["children"]] == ["Big"] and films["small_folders"] == 10
    assert u["largest_files"][0]["path"] == "Films/Big/movie.mkv"
    assert c.get("/api/usage/children?rel=nope").status_code == 404
    # survives restart
    svc2 = Services(env["cfg"], mounts_file=str(env["mounts"]))
    assert svc2.usage.summary()["total"] == u["root"]["s"]
