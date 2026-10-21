import json
import os
import time
import zipfile

import pytest

from helpers import build_cr2, put
from photomanager import takeout as tk
from test_develop import library, stub_calls, wait_develop
from test_thumbs_google import FakeGoogle, connect, g, wait_upload  # noqa: F401  (g is a fixture)

TS = tk.taken_ts("2023-01-01 10:00:00")          # IMG_0001
TS2 = tk.taken_ts("2023-06-01 10:00:00")         # IMG_0002
TS3 = tk.taken_ts("2024-03-01 10:00:00")         # IMG_0003


def sidecar(title, ts, **extra):
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": str(ts)}, **extra}).encode()


def make_zip(path, entries, extra_files=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
        for name, data in extra_files:
            z.writestr(name, data)
    return path


def takeout_dir(env):
    return env["nas"] / "Takeout"


def scan_takeout(env, timeout=20):
    r = env["c"].post("/api/takeout/folder", json={"folder": "Takeout"})
    assert r.status_code == 200, r.json
    assert env["c"].post("/api/takeout/scan").status_code == 200
    end = time.time() + timeout
    while time.time() < end:
        st = env["c"].get("/api/takeout").json
        if not st["state"]["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("takeout scan did not finish")


def standard_export(env):
    d = takeout_dir(env)
    make_zip(d / "takeout-001.zip", [
        ("Takeout/Google Photos/Photos from 2023/IMG_0001.JPG.json", sidecar("IMG_0001.JPG", TS)),
        ("Takeout/Google Photos/Photos from 2023/IMG_0001.JPG", b"\xff\xd8 media bytes"),
        ("Takeout/Google Photos/Photos from 2023/other.jpg.json", sidecar("IMG_9999.jpg", TS2 + 1)),
        ("Takeout/Google Photos/Photos from 2020/IMG_0003.JPG.json", sidecar("IMG_0003.JPG", tk.taken_ts("2020-03-01 10:00:00"))),
    ], extra_files=[("Takeout/Google Photos/Photos from 2023/metadata.json", b'{"title": "Photos from 2023"}'),
                    ("Takeout/Google Photos/Trash/old.jpg.json", sidecar("trashed.jpg", TS3, trashed=True))])


# --- reading sidecars -----------------------------------------------------------------------------------
@pytest.mark.parametrize("title, stem", [
    ("IMG_0001.JPG", "img_0001"), ("img_0001.cr2", "img_0001"), ("IMG_0001_developed.jpg", "img_0001"),
    ("IMG_0001(1).jpg", "img_0001"), ("IMG_0001-edited.jpg", "img_0001"), ("IMG_0001 (2).JPG", "img_0001"),
    ("no extension", "no extension"), ("a.b.c.jpg", "a.b.c")])
def test_names_are_normalised(title, stem):
    assert tk.normalise(title) == stem


def test_sidecar_parsing():
    assert tk.parse_sidecar(sidecar("IMG_1.JPG", 5)) == ("img_1", "IMG_1.JPG", 5)
    for bad in (b"", b"not json", b"[]", b'{"title": "x"}', sidecar("", 5), sidecar("x", "soon"),
                sidecar("x", 5, trashed=True), b'{"title": 5, "photoTakenTime": {"timestamp": "1"}}',
                json.dumps({"title": "x", "photoTakenTime": {}}).encode()):
        assert tk.parse_sidecar(bad) is None, bad


def test_taken_ts_is_the_wall_clock_read_as_utc():
    assert tk.taken_ts("1970-01-02 00:00:00") == 86400
    assert tk.taken_ts(None) is None and tk.taken_ts("garbage") is None


# --- scanning the export -------------------------------------------------------------------------------------
def test_scan_reads_only_sidecars_from_zips_and_folders(env):
    library(env)
    standard_export(env)
    put(takeout_dir(env) / "extracted" / "IMG_5.jpg.json", sidecar("IMG_5.jpg", 12345))
    put(takeout_dir(env) / "extracted" / "IMG_5.jpg", b"\xff\xd8 big media")
    put(takeout_dir(env) / "extracted" / "huge.json", b" " * (tk.MAX_JSON + 1))
    before = sorted((str(p), p.stat().st_mtime) for p in takeout_dir(env).rglob("*"))
    st = scan_takeout(env)
    assert st["state"]["error"] is None and st["state"]["errors"] == 0
    assert st["counts"] == {"sources": 3, "entries": 4}          # 3 in the zip (not metadata/trashed) + 1 loose; huge.json yields none
    assert st["state"]["finished"] and st["folder"] == "Takeout" and st["folder_ok"]
    assert sorted((str(p), p.stat().st_mtime) for p in takeout_dir(env).rglob("*")) == before    # read-only


def test_rescan_is_incremental_and_follows_changes(env):
    library(env)
    standard_export(env)
    assert scan_takeout(env)["counts"]["entries"] == 3
    # unchanged: nothing is read again (prove it by breaking the zip's reader)
    orig = tk.Takeout._read_zip
    tk.Takeout._read_zip = lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-read"))
    try:
        assert scan_takeout(env)["counts"]["entries"] == 3
    finally:
        tk.Takeout._read_zip = orig
    # a changed file is read again, a removed one is forgotten, a new one is added
    z = takeout_dir(env) / "takeout-001.zip"
    make_zip(z, [("a/x.json", sidecar("NEW.JPG", 99))])
    os.utime(z, (time.time() + 5, time.time() + 5))
    put(takeout_dir(env) / "later" / "L.json", sidecar("L.jpg", 100))
    st = scan_takeout(env)
    assert st["counts"] == {"sources": 2, "entries": 2}
    (takeout_dir(env) / "later" / "L.json").unlink()
    assert scan_takeout(env)["counts"] == {"sources": 1, "entries": 1}


def test_a_corrupt_zip_is_reported_but_doesnt_stop_the_scan(env):
    library(env)
    standard_export(env)
    put(takeout_dir(env) / "broken.zip", b"PK\x03\x04 this is not really a zip")
    st = scan_takeout(env)
    assert st["state"]["errors"] == 1 and "broken.zip" in st["state"]["error_samples"][0]
    assert st["counts"]["entries"] == 3


def test_takeout_folder_validation_and_prerequisites(env):
    c = env["c"]
    assert c.post("/api/takeout/scan").status_code == 400                  # no folder chosen
    assert c.get("/api/takeout/missing").status_code == 400                # nothing scanned
    for bad in ("nope", "../x", "/"):
        assert c.post("/api/takeout/folder", json={"folder": bad}).status_code == 400, bad
    takeout_dir(env).mkdir()
    assert c.post("/api/takeout/folder", json={"folder": "Takeout"}).json["folder"] == "Takeout"
    assert c.post("/api/takeout/folder", json={"folder": ""}).json["folder"] is None


# --- what is missing --------------------------------------------------------------------------------------------
def test_matching_name_time_and_recurring_names(env):
    library(env)
    standard_export(env)
    scan_takeout(env)
    r = env["c"].get("/api/takeout/missing").json
    # IMG_0001: same name + time -> uploaded.  IMG_0002: only a photo at the same second -> possibly.
    # IMG_0003: the name exists but 3 years earlier (a recurring counter) -> missing.
    assert r["counts"] == {"in_scope": 3, "uploaded": 1, "possibly": 1, "missing": 1}
    assert [i["rel"] for i in r["items"]] == ["2024/IMG_0003.CR2"] and r["total"] == 1
    r = env["c"].get("/api/takeout/missing?possible=1").json
    assert [(i["rel"], i["match"]) for i in r["items"]] == [("2024/IMG_0003.CR2", "missing"),
                                                            ("2023/IMG_0002.CR2", "possibly")]
    assert r["folders"] == [{"path": "2023", "count": 1}, {"path": "2024", "count": 1}] and r["folders_total"] == 2


def test_photos_without_a_capture_time_are_never_guessed_to_be_uploaded(env):
    library(env, extra=[("2025/NOEXIF.CR2", build_cr2(taken=None, pad=5))])
    standard_export(env)
    scan_takeout(env)
    items = {i["rel"] for i in env["c"].get("/api/takeout/missing").json["items"]}
    assert "2025/NOEXIF.CR2" in items


def test_folder_scope_and_paging(env):
    library(env)
    standard_export(env)
    scan_takeout(env)
    r = env["c"].get("/api/takeout/missing?folder=2023&possible=1").json
    assert r["counts"]["in_scope"] == 2 and r["total"] == 1 and r["items"][0]["rel"] == "2023/IMG_0002.CR2"
    r = env["c"].get("/api/takeout/missing?possible=1&limit=1&offset=1").json
    assert r["total"] == 2 and len(r["items"]) == 1 and r["items"][0]["rel"] == "2023/IMG_0002.CR2"


def test_resolve_picks_files_and_folders_but_only_really_missing_ones(env):
    library(env, extra=[("2024/IMG_0004.CR2", build_cr2(taken="2024:09:01 10:00:00", pad=44)),
                        ("IMG_ROOT.CR2", build_cr2(taken="2025:01:01 10:00:00", pad=45))])
    standard_export(env)
    scan_takeout(env)
    t = env["svc"].takeout
    assert t.resolve(["2024/IMG_0003.CR2"], [], False) == ["2024/IMG_0003.CR2"]
    assert t.resolve([], ["2024"], False) == ["2024/IMG_0004.CR2", "2024/IMG_0003.CR2"]
    assert t.resolve([], [""], False) == ["IMG_ROOT.CR2"]                       # directly in the photo folder
    assert t.resolve(["2023/IMG_0001.CR2"], [], False) == []                    # already in Google Photos
    assert t.resolve(["2023/IMG_0002.CR2"], [], False) == []                    # only "possibly"
    assert t.resolve(["2023/IMG_0002.CR2"], [], True) == ["2023/IMG_0002.CR2"]
    assert t.resolve(["nope.CR2", 5], [None], False) == [] and t.resolve([], [], False) == []


# --- develop (if needed) and upload the selection ---------------------------------------------------------------
def wait_job(env, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = env["c"].get("/api/takeout").json["job"]
        if not j["running"]:
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_selection_develops_what_is_needed_then_uploads_only_that(env, g):
    library(env)
    standard_export(env)
    scan_takeout(env)
    connect(env)
    # scope and skip settings must not get in the way of an explicit choice
    env["c"].post("/api/develop/settings", json={"scope": "2023", "skip_with_jpeg": True})
    r = env["c"].post("/api/takeout/upload", json={"folders": ["2024"], "rels": ["2023/IMG_0002.CR2"],
                                                   "possible": True})
    assert r.status_code == 200 and r.json["total"] == 2 and r.json["to_develop"] == 2
    job = wait_job(env)
    assert job["error"] is None and job["developed"] == 2 and job["uploaded"] == 2 and job["failed"] == 0
    assert len(g.uploads) == 2 and len(stub_calls(env)) == 2
    out = env["nas"] / "PhotoManager" / "Processed"
    assert sorted(p.name for p in out.rglob("*.jpg")) == ["IMG_0002_developed.jpg", "IMG_0003_developed.jpg"]
    # they now count as uploaded, so they drop off the list
    r = env["c"].get("/api/takeout/missing?possible=1").json
    assert r["total"] == 0 and r["counts"]["uploaded"] == 3
    # a second go with the same selection has nothing to do
    assert env["c"].post("/api/takeout/upload", json={"rels": ["2024/IMG_0003.CR2"]}).status_code == 400


def test_already_developed_photos_are_uploaded_without_developing_again(env, g):
    library(env)
    standard_export(env)
    scan_takeout(env)
    connect(env)
    env["c"].post("/api/develop/run")
    wait_develop(env)
    n = len(stub_calls(env))
    r = env["c"].post("/api/takeout/upload", json={"rels": ["2024/IMG_0003.CR2"]})
    assert r.json["to_develop"] == 0
    job = wait_job(env)
    assert job["uploaded"] == 1 and len(stub_calls(env)) == n and len(g.uploads) == 1


def test_upload_selection_refusals(env, g):
    library(env)
    standard_export(env)
    scan_takeout(env)
    c = env["c"]
    r = c.post("/api/takeout/upload", json={"rels": ["2024/IMG_0003.CR2"]})
    assert r.status_code == 400 and "Connect Google Photos" in r.json["error"]
    connect(env)
    assert c.post("/api/takeout/upload", json={}).status_code == 400
    assert "Nothing is selected" in c.post("/api/takeout/upload", json={"rels": ["2023/IMG_0001.CR2"]}).json["error"]
    env["proc"].state["running"] = True
    r = c.post("/api/takeout/upload", json={"rels": ["2024/IMG_0003.CR2"]})
    assert r.status_code == 409
    env["proc"].state["running"] = False


def test_a_photo_that_cannot_be_developed_is_skipped_and_reported(env, g):
    library(env, extra=[("2024/BAD.CR2", build_cr2(taken="2024:10:01 10:00:00", pad=3) + b"FAILME")])
    standard_export(env)
    scan_takeout(env)
    connect(env)
    c = env["c"]
    r = c.post("/api/takeout/upload", json={"rels": ["2024/BAD.CR2", "2024/IMG_0003.CR2"]})
    assert r.status_code == 200
    job = wait_job(env)
    assert job["developed"] == 1 and job["uploaded"] == 1 and len(g.uploads) == 1


def test_cancel_stops_the_job(env, g):
    library(env, extra=[("2024/SLOW.CR2", build_cr2(taken="2024:10:01 10:00:00", pad=3) + b"SLOW")])
    standard_export(env)
    scan_takeout(env)
    connect(env)
    env["c"].post("/api/takeout/upload", json={"rels": ["2024/SLOW.CR2"]})
    end = time.time() + 10
    while time.time() < end and not stub_calls(env):
        time.sleep(0.05)
    assert env["c"].post("/api/takeout/upload/cancel").json == {"ok": True}
    job = wait_job(env, 15)
    assert job["stopped"] == "Stopped" and job["uploaded"] == 0 and not g.uploads
