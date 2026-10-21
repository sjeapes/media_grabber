import json
import os
import sys
import time
from pathlib import Path

import pytest

from helpers import build_cr2, choose_photos, put, run_scan, tree, wait_scan

from photomanager import scanner  # noqa: E402
from photomanager.web import Services  # noqa: E402


# --- EXIF ----------------------------------------------------------------------
def test_read_exif_reads_time_and_camera(tmp_path):
    p = put(tmp_path / "a.CR2", build_cr2())
    m = scanner.read_exif(str(p))
    assert m["taken"] == "2024-05-01 10:00:00"
    assert m["camera"] == "Canon EOS 5D Mark III"
    assert m["error"] is None


def test_read_exif_adds_make_when_model_lacks_it(tmp_path):
    p = put(tmp_path / "a.cr2", build_cr2(make="Canon", model="PowerShot G7 X"))
    assert scanner.read_exif(str(p))["camera"] == "Canon PowerShot G7 X"


def test_read_exif_survives_garbage_and_missing_time(tmp_path):
    bad = put(tmp_path / "bad.cr2", b"this is not a tiff at all")
    m = scanner.read_exif(str(bad))
    assert m["taken"] is None and m["error"]
    no_time = put(tmp_path / "nt.cr2", build_cr2(taken=None))
    m = scanner.read_exif(str(no_time))
    assert m["taken"] is None and m["camera"] == "Canon EOS 5D Mark III"
    assert scanner.read_exif(str(tmp_path / "missing.cr2"))["error"]


def test_parse_exif_time_rejects_nonsense():
    assert scanner.parse_exif_time("2024:13:45 99:00:00") is None
    assert scanner.parse_exif_time("0000:00:00 00:00:00") is None
    assert scanner.parse_exif_time("2024:05:01 10:00:00") == "2024-05-01 10:00:00"


# --- access ----------------------------------------------------------------------
def test_only_ingress_clients(env):
    c = env["c"]
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.1.20"}).status_code == 403
    assert c.get("/", environ_base={"REMOTE_ADDR": "192.168.1.20"}).status_code == 403


# --- NAS detection -----------------------------------------------------------------
def test_single_nas_is_detected_automatically(env):
    s = env["c"].get("/api/status").json["nas"]
    assert s["healthy"] and s["fstype"] == "cifs"
    assert s["choice"] == "auto" and s["name"] == "NAS" and s["path"] == str(env["nas"])
    assert s["disk"]["total_gb"] > 0
    assert not s["ready"], "not ready until a photo folder is chosen"
    assert any("photo folder" in w for w in s["warnings"])


def test_no_nas_means_not_ready_and_never_local_disk(env):
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    s = env["c"].get("/api/status").json["nas"]
    assert not s["healthy"] and s["path"] is None
    assert "No network storage found" in s["problems"][0]
    r = env["c"].post("/api/scan")
    assert r.status_code == 400
    assert not (env["media"] / "PhotoManager").exists()


def test_two_nas_need_a_choice(env):
    second = env["media"] / "Backup"
    second.mkdir()
    env["mounts"].write_text(env["mounts"].read_text() + f"//192.168.1.176/b {second} nfs4 rw 0 0\n")
    s = env["c"].get("/api/status").json["nas"]
    assert s["needs_choice"] and not s["healthy"]
    opts = env["c"].get("/api/storage/options").json
    assert [c["name"] for c in opts["candidates"]] == ["Backup", "NAS"]
    assert env["c"].post("/api/storage/select", json={"choice": "/etc"}).status_code == 400
    s = env["c"].post("/api/storage/select", json={"choice": str(env["nas"])}).json
    assert s["healthy"] and s["path"] == str(env["nas"])
    # the choice survives a restart
    svc2 = Services(env["cfg"], mounts_file=str(env["mounts"]))
    assert svc2.storage.resolve()["path"] == str(env["nas"])


def test_selected_nas_disappearing_is_reported(env):
    env["c"].post("/api/storage/select", json={"choice": str(env["nas"])})
    env["mounts"].write_text("overlay / overlay rw 0 0\n")
    s = env["c"].get("/api/status").json["nas"]
    assert not s["healthy"] and "isn't mounted" in s["problems"][0]


# --- folder browser ----------------------------------------------------------------
def test_folder_browser_lists_real_folders_only(env):
    nas = env["nas"]
    for d in ("Pictures/Canon", "Pictures/Phone", "media", "@eaDir", "#recycle", ".hidden"):
        (nas / d).mkdir(parents=True, exist_ok=True)
    put(nas / "Pictures" / "readme.txt", b"x")
    root = env["c"].get("/api/folders").json
    assert root["is_root"] and root["dirs"] == ["media", "Pictures"] and root["parent"] is None
    pics = env["c"].get("/api/folders", query_string={"rel": "Pictures"}).json
    assert pics["dirs"] == ["Canon", "Phone"] and pics["parent"] == "" and pics["cr2_here"] == 0
    put(nas / "Pictures" / "Canon" / "IMG_1.CR2", build_cr2())
    put(nas / "Pictures" / "Canon" / "IMG_1.JPG", b"jpg")
    put(nas / "Pictures" / "Canon" / ".IMG_2.CR2", build_cr2())
    canon = env["c"].get("/api/folders", query_string={"rel": "Pictures/Canon"}).json
    assert canon["cr2_here"] == 1 and canon["parent"] == "Pictures"


@pytest.mark.parametrize("rel", ["../etc", "Pictures/../../etc", "/etc", "..", "a/./b"])
def test_folder_browser_blocks_traversal(env, rel):
    (env["nas"] / "Pictures").mkdir()
    r = env["c"].get("/api/folders", query_string={"rel": rel})
    assert r.status_code == 400


def test_folder_browser_blocks_symlink_escape(env):
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (env["nas"] / "link").symlink_to(outside)
    r = env["c"].get("/api/folders", query_string={"rel": "link"})
    assert r.status_code == 400 and "outside" in r.json["error"]


def test_folder_browser_missing_folder(env):
    r = env["c"].get("/api/folders", query_string={"rel": "Nope"})
    assert r.status_code == 400 and "Can't open" in r.json["error"]


# --- folder settings ---------------------------------------------------------------
def test_choose_photo_folder_and_defaults(env):
    (env["photos"]).mkdir(parents=True)
    s = choose_photos(env).json
    assert s["ready"] and s["photo_folder_ok"]
    f = s["folders"]
    assert f["photo"]["rel"] == "Pictures/Canon" and f["photo"]["path"] == str(env["photos"])
    assert f["processed"]["rel"] == "PhotoManager/Processed"
    assert f["quarantine"]["rel"] == "PhotoManager/Quarantine"
    assert not (env["nas"] / "PhotoManager").exists(), "nothing is created on the NAS yet"
    # persisted relative to the NAS, so it survives a restart
    svc2 = Services(env["cfg"], mounts_file=str(env["mounts"]))
    assert svc2.storage.settings["photo_folder"] == "Pictures/Canon"


def test_folder_choice_validation(env):
    env["photos"].mkdir(parents=True)
    c = env["c"]
    assert c.post("/api/folders", json={"photo": "Nope/Missing"}).status_code == 400
    assert c.post("/api/folders", json={"photo": ""}).status_code == 400
    assert c.post("/api/folders", json={"photo": "../x"}).status_code == 400
    choose_photos(env)
    for payload in ({"processed": "Pictures/Canon/Out"},      # inside the photo folder
                    {"processed": "Pictures"},                 # contains the photo folder
                    {"quarantine": "PhotoManager/Processed"},  # same as processed
                    {"processed": "PhotoManager/Quarantine"}):
        r = c.post("/api/folders", json=payload)
        assert r.status_code == 400, payload
        assert "can't be the same or inside each other" in r.json["error"]
    ok = c.post("/api/folders", json={"processed": "Developed", "quarantine": "Rejects/Quarantine"})
    assert ok.status_code == 200
    assert ok.json["folders"]["processed"]["rel"] == "Developed"


def test_photo_folder_vanishing_is_a_problem(env):
    env["photos"].mkdir(parents=True)
    choose_photos(env)
    env["photos"].rmdir()
    s = env["c"].get("/api/status").json["nas"]
    assert not s["ready"] and not s["healthy"] and "choose it again" in s["problems"][0]


# --- scanning ----------------------------------------------------------------------
def test_scan_needs_a_photo_folder(env):
    r = env["c"].post("/api/scan")
    assert r.status_code == 400 and "photo folder" in r.json["error"]


def test_scan_reports_counts_cameras_years_pairs(env):
    p = env["photos"]
    put(p / "2023" / "IMG_0001.CR2", build_cr2(taken="2023:07:04 09:00:00", pad=10))
    put(p / "2023" / "IMG_0001.JPG", b"camera jpeg")
    put(p / "2023" / "IMG_0002.cr2", build_cr2(taken="2023:07:04 09:00:05", pad=20))
    put(p / "2024" / "IMG_0003.CR2", build_cr2(taken="2024:01:02 03:04:05", pad=30))
    put(p / "2024" / "notes.txt", b"ignore me")
    put(p / "2024" / "IMG_0004.NEF", b"not canon, ignored")
    put(p / "2024" / ".IMG_0005.CR2", build_cr2())
    put(p / "@eaDir" / "x" / "IMG_9999.CR2", build_cr2())
    choose_photos(env)
    rep = run_scan(env)
    assert rep["total_files"] == 3
    assert rep["first"] == "2023-07-04 09:00:00" and rep["last"] == "2024-01-02 03:04:05"
    assert rep["cameras"] == [{"name": "Canon EOS 5D Mark III", "files": 3}]
    assert rep["years"] == [{"year": "2023", "files": 2}, {"year": "2024", "files": 1}]
    assert rep["with_jpeg"] == 1 and rep["dated_from_file"] == 0
    assert {f["path"]: f["files"] for f in rep["folders"]} == {"2023": 2, "2024": 1}
    assert rep["duplicates"]["groups"] == 0
    assert rep["total_gb"] >= 0


def test_scan_finds_exact_duplicates_only(env):
    p = env["photos"]
    same = build_cr2(pad=100)
    put(p / "a" / "IMG_1.CR2", same)
    put(p / "b" / "copy of IMG_1.CR2", same)
    put(p / "c" / "IMG_1 (2).CR2", same)
    # same size and same EXIF, different bytes: must NOT be reported as a duplicate
    other = bytearray(same)
    other[-1] ^= 0xFF
    put(p / "d" / "IMG_2.CR2", bytes(other))
    put(p / "e" / "IMG_3.CR2", build_cr2(pad=7))
    choose_photos(env)
    rep = run_scan(env)
    d = rep["duplicates"]
    assert rep["total_files"] == 5
    assert d["groups"] == 1 and d["extra_files"] == 2
    assert d["top"][0]["count"] == 3
    assert d["top"][0]["files"] == ["a/IMG_1.CR2", "b/copy of IMG_1.CR2", "c/IMG_1 (2).CR2"]
    assert d["reclaimable_gb"] == pytest.approx(2 * len(same) / 1024 ** 3, abs=0.01)


def test_files_that_cant_be_read_fall_back_to_file_time(env):
    p = env["photos"]
    mt = time.mktime((2020, 2, 3, 4, 5, 6, 0, 0, -1))
    put(p / "broken.CR2", b"garbage", mtime=mt)
    choose_photos(env)
    rep = run_scan(env)
    assert rep["total_files"] == 1 and rep["dated_from_file"] == 1
    # a guessed (file) date must not pose as a capture date in the range or year list
    assert rep["first"] is None and rep["last"] is None
    assert rep["years"] == [{"year": "Unknown", "files": 1}]
    assert rep["state"]["errors"] == 1
    assert rep["cameras"] == [{"name": "Unknown", "files": 1}]


def test_scan_never_modifies_the_photo_folder(env):
    p = env["photos"]
    same = build_cr2(pad=50)
    put(p / "a" / "IMG_1.CR2", same, mtime=1_600_000_000)
    put(p / "b" / "IMG_1.CR2", same, mtime=1_600_000_100)
    put(p / "b" / "IMG_1.JPG", b"x", mtime=1_600_000_200)
    choose_photos(env)
    before = tree(env["nas"])
    run_scan(env)
    run_scan(env)
    assert tree(env["nas"]) == before


def test_rescan_only_reads_new_or_changed_files(env, monkeypatch):
    p = env["photos"]
    put(p / "IMG_1.CR2", build_cr2(pad=1), mtime=1_600_000_000)
    put(p / "IMG_2.CR2", build_cr2(pad=2), mtime=1_600_000_000)
    choose_photos(env)
    run_scan(env)
    reads = []
    real = scanner.read_exif
    monkeypatch.setattr(scanner, "read_exif", lambda path: reads.append(Path(path).name) or real(path))
    run_scan(env)
    assert reads == []
    put(p / "IMG_3.CR2", build_cr2(pad=3))
    put(p / "IMG_2.CR2", build_cr2(pad=22, taken="2025:01:01 00:00:00"), mtime=1_700_000_000)
    rep = run_scan(env)
    assert sorted(reads) == ["IMG_2.CR2", "IMG_3.CR2"]
    assert rep["total_files"] == 3 and rep["last"] == "2025-01-01 00:00:00"


def test_rescan_forgets_deleted_files_and_changed_duplicates(env):
    p = env["photos"]
    same = build_cr2(pad=9)
    put(p / "a.CR2", same, mtime=1_600_000_000)
    put(p / "b.CR2", same, mtime=1_600_000_000)
    choose_photos(env)
    assert run_scan(env)["duplicates"]["groups"] == 1
    (p / "b.CR2").unlink()
    rep = run_scan(env)
    assert rep["total_files"] == 1 and rep["duplicates"]["groups"] == 0
    # a file replaced by different content of the same size is no longer a duplicate
    put(p / "b.CR2", same, mtime=1_600_000_000)
    assert run_scan(env)["duplicates"]["groups"] == 1
    changed = bytearray(same)
    changed[-1] ^= 1
    put(p / "b.CR2", bytes(changed), mtime=1_600_000_500)
    assert run_scan(env)["duplicates"]["groups"] == 0


def test_a_different_photo_folder_is_a_different_library(env):
    put(env["photos"] / "IMG_1.CR2", build_cr2())
    put(env["nas"] / "Pictures" / "Old" / "IMG_7.CR2", build_cr2(model="Canon EOS 40D"))
    put(env["nas"] / "Pictures" / "Old" / "IMG_8.CR2", build_cr2(model="Canon EOS 40D", pad=4))
    choose_photos(env)
    assert run_scan(env)["total_files"] == 1
    env["c"].post("/api/folders", json={"photo": "Pictures/Old"})
    rep = run_scan(env)
    assert rep["total_files"] == 2 and rep["cameras"][0]["name"] == "Canon EOS 40D"


def test_empty_folder_gives_zero_not_unknown(env):
    env["photos"].mkdir(parents=True)
    choose_photos(env)
    rep = run_scan(env)
    assert rep["total_files"] == 0


def test_cannot_start_two_scans_and_cancel_works(env, monkeypatch):
    p = env["photos"]
    for i in range(30):
        put(p / f"IMG_{i}.CR2", build_cr2(pad=i))
    choose_photos(env)
    real = scanner.read_exif

    def slow(path):
        time.sleep(0.05)
        return real(path)
    monkeypatch.setattr(scanner, "read_exif", slow)
    assert env["c"].post("/api/scan").status_code == 200
    assert env["c"].post("/api/scan").status_code == 409
    env["c"].post("/api/scan/cancel")
    st = wait_scan(env["c"])
    assert st["cancelled"] and not st["error"] and not st["running"]
    assert st["files"] < 30
    # a later scan completes normally
    monkeypatch.setattr(scanner, "read_exif", real)
    assert run_scan(env)["total_files"] == 30


def test_last_scan_survives_restart(env):
    put(env["photos"] / "IMG_1.CR2", build_cr2())
    choose_photos(env)
    run_scan(env)
    svc2 = Services(env["cfg"], mounts_file=str(env["mounts"]))
    rep = svc2.scanner.report()
    assert rep["total_files"] == 1 and rep["state"]["finished"] and not rep["state"]["running"]


def test_scan_reports_unreadable_nas_folder_errors(env, monkeypatch):
    put(env["photos"] / "ok" / "IMG_1.CR2", build_cr2())
    (env["photos"] / "locked").mkdir()
    choose_photos(env)
    real = os.scandir

    def flaky(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(scanner.os, "scandir", flaky)
    rep = run_scan(env)
    assert rep["total_files"] == 1 and rep["state"]["errors"] == 1


# --- Home Assistant ----------------------------------------------------------------
def test_publishes_entities_to_home_assistant(env):
    posted = {}
    ha = env["svc"].ha
    ha.enabled = True
    ha._post = lambda path, body: posted.__setitem__(path.rsplit("/", 1)[-1].split(".", 1)[-1], body)
    same = build_cr2(pad=5)
    put(env["photos"] / "a.CR2", same)
    put(env["photos"] / "b.CR2", same)
    choose_photos(env)
    run_scan(env)
    env["svc"].publish()
    assert posted["photo_manager_storage_problem"]["state"] == "off"
    assert posted["photo_manager_status"]["state"] == "idle"
    photos = posted["photo_manager_photos"]
    assert photos["state"] == 2
    assert photos["attributes"]["duplicate_groups"] == 1
    assert photos["attributes"]["cameras"] == [{"name": "Canon EOS 5D Mark III", "files": 2}]


def test_no_photos_sensor_before_the_first_scan(env):
    posted = {}
    ha = env["svc"].ha
    ha.enabled = True
    ha._post = lambda path, body: posted.__setitem__(path.rsplit("/", 1)[-1].split(".", 1)[-1], body)
    env["svc"].publish()
    assert "photo_manager_photos" not in posted
    assert posted["photo_manager_status"]["state"] == "idle"
