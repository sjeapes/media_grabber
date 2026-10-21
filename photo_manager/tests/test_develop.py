import json
import os
import time
from datetime import datetime

import pytest

from helpers import build_cr2, choose_photos, put, run_scan, tree
from photomanager import processor as processor_mod
from photomanager.web import Services

IN_WINDOW = datetime(2026, 10, 21, 2, 0)       # 02:00 — inside the default 01:00–06:00 window
OUT_OF_WINDOW = datetime(2026, 10, 21, 12, 0)


def wait_develop(env, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not env["proc"].state["running"]:
            return env["proc"].state
        time.sleep(0.05)
    raise AssertionError("develop run did not finish")


def enable(env, on=True, mode=None):
    body = {"enabled": on}
    if mode:
        body["output_mode"] = mode
    r = env["c"].post("/api/develop/settings", json=body)
    assert r.status_code == 200, r.json
    return r.json


def develop_now(env):
    r = env["c"].post("/api/develop/run")
    assert r.status_code == 200, r.json
    return wait_develop(env)


def stub_calls(env):
    if not env["stub_log"].exists():
        return []
    return [json.loads(line) for line in env["stub_log"].read_text().splitlines()]


def files_only(root):
    """(rel, size, mtime) for files only; a folder's mtime changes when files are added to it."""
    return [(r, s, m) for r, s, m in tree(root) if (root / r).is_file()]


def library(env, extra=()):
    """Three CR2s in two folders (newest first by capture date: c, b, a)."""
    p = env["photos"]
    put(p / "2023" / "IMG_0001.CR2", build_cr2(taken="2023:01:01 10:00:00", pad=11))
    put(p / "2023" / "IMG_0002.CR2", build_cr2(taken="2023:06:01 10:00:00", pad=22))
    put(p / "2024" / "IMG_0003.CR2", build_cr2(taken="2024:03:01 10:00:00", pad=33))
    for rel, data in extra:
        put(p / rel, data)
    choose_photos(env)
    run_scan(env)


# --- off by default -----------------------------------------------------------------------------
def test_automatic_developing_is_off_by_default_and_the_scheduler_does_nothing(env):
    library(env)
    d = env["c"].get("/api/develop").json
    assert d["enabled"] is False and d["output_mode"] == "separate"
    before = tree(env["nas"])
    env["proc"].clock = lambda: IN_WINDOW
    assert env["proc"].scheduled_tick() is False      # even inside the overnight window
    time.sleep(0.2)
    assert not env["proc"].state["running"]
    assert tree(env["nas"]) == before and not env["stub_log"].exists()


def test_develop_now_works_with_the_automatic_switch_off(env):
    library(env)
    assert env["c"].get("/api/develop").json["enabled"] is False
    st = develop_now(env)                              # no enable() call
    assert st["done"] == 3 and st["trigger"] == "manual" and not st["stop_reason"]
    assert len(stub_calls(env)) == 3
    # …but the overnight run still needs the switch
    with pytest.raises(processor_mod.DevelopError, match="Automatic developing is turned off"):
        env["proc"].start("schedule", window="2026-10-21")


def test_toggle_persists_and_odd_values_mean_off(env):
    assert enable(env)["enabled"] is True
    assert Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["processing_enabled"] is True
    assert enable(env, False)["enabled"] is False
    for bad in ("yes", 1, "true"):
        r = env["c"].post("/api/develop/settings", json={"enabled": bad})
        assert r.status_code == 400
    (env["cfg"].data_dir / "settings.json").write_text(json.dumps({"processing_enabled": "yes"}))
    assert Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["processing_enabled"] is False
    (env["cfg"].data_dir / "settings.json").write_text(json.dumps({"output_mode": "sideways"}))
    assert Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["output_mode"] == "separate"


def test_status_shows_develop_summary(env):
    s = env["c"].get("/api/status").json
    assert s["develop"] == {"running": False, "done": 0, "total": 0, "phase": None, "enabled": False}
    assert s["nas"]["processing_enabled"] is False and s["nas"]["output_mode"] == "separate"


def test_run_needs_darktable_and_a_photo_folder(env):
    r = env["c"].post("/api/develop/run")
    assert r.status_code == 400 and "photo folder" in r.json["error"]
    env["cfg"].darktable = "no-such-darktable"
    env["proc"]._dt_info = None
    assert env["c"].get("/api/develop").json["darktable"]["available"] is False
    r = env["c"].post("/api/develop/run")
    assert r.status_code == 400 and "isn't installed" in r.json["error"]


# --- separate folder ----------------------------------------------------------------------------
def test_develop_into_a_separate_folder(env):
    library(env)
    enable(env)
    photos_before = tree(env["photos"])
    st = develop_now(env)
    assert st["done"] == 3 and st["failed"] == 0 and not st["stop_reason"] and not st["error"]
    out = env["nas"] / "PhotoManager" / "Processed"
    names = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert names == ["2023/IMG_0001_developed.jpg", "2023/IMG_0002_developed.jpg",
                     "2024/IMG_0003_developed.jpg"]
    assert all(p.read_bytes()[:2] == b"\xff\xd8" for p in out.rglob("*.jpg"))
    # the photo folder is exactly as it was: no JPEGs, no sidecars, no partial files
    assert tree(env["photos"]) == photos_before
    assert not [p for p in env["nas"].rglob("*") if p.suffix in (".xmp", ".partial")
                and p.is_file()]
    # darktable ran on private copies, headless, with the safe flags
    calls = stub_calls(env)
    assert len(calls) == 3
    for c in calls:
        assert str(env["nas"]) not in c["src_dir"] and str(env["cfg"].tmp_dir) in c["src_dir"]
        assert str(env["cfg"].tmp_dir) in c["home"]
        a = c["args"]
        assert a[a.index("--library") + 1] == ":memory:"
        assert "--disable-opencl" in a and "write_sidecar_files=never" in a
        assert "plugins/imageio/format/jpeg/quality=92" in a
    assert not (env["cfg"].tmp_dir / "work").exists()
    c = env["c"].get("/api/develop").json["counts"]
    assert (c["developed"], c["pending"], c["failed"]) == (3, 0, 0)
    # newest photo first
    assert calls[0]["size"] == (env["photos"] / "2024" / "IMG_0003.CR2").stat().st_size
    # nothing left to do: a second run develops nothing and doesn't call darktable
    assert develop_now(env)["total"] == 0 and len(stub_calls(env)) == 3


def test_jpeg_quality_option_is_passed_to_darktable(env):
    env["cfg"].options["jpeg_quality"] = 80
    library(env)
    enable(env)
    develop_now(env)
    assert "plugins/imageio/format/jpeg/quality=80" in stub_calls(env)[0]["args"]


# --- next to the RAW file -------------------------------------------------------------------------
def test_develop_next_to_the_raw_file_never_touches_the_camera_jpeg(env):
    p = env["photos"]
    put(p / "2023" / "IMG_0001.JPG", b"camera jpeg bytes")
    library(env)
    assert env["c"].get("/api/report").json["with_jpeg"] == 1
    enable(env, mode="alongside")
    before = files_only(env["photos"])
    st = develop_now(env)
    assert st["done"] == 3
    assert (p / "2023" / "IMG_0001.JPG").read_bytes() == b"camera jpeg bytes"
    for sub, stem in (("2023", "IMG_0001"), ("2023", "IMG_0002"), ("2024", "IMG_0003")):
        assert (p / sub / f"{stem}_developed.jpg").read_bytes()[:2] == b"\xff\xd8"
    after = files_only(env["photos"])
    added = sorted({r[0] for r in after} - {r[0] for r in before})
    assert added == ["2023/IMG_0001_developed.jpg", "2023/IMG_0002_developed.jpg",
                     "2024/IMG_0003_developed.jpg"]
    assert [r for r in before if r in after] == before, "no existing file changed"
    assert not (env["nas"] / "PhotoManager" / "Processed").exists()
    # the new JPEGs are not mistaken for camera JPEGs, and aren't scanned as photos
    rep = run_scan(env)
    assert rep["total_files"] == 3 and rep["with_jpeg"] == 1


def test_where_the_jpeg_goes_is_shown_for_both_modes(env):
    library(env)
    d = env["c"].get("/api/develop").json
    assert d["example"] == {"raw": "Pictures/Canon/2024/IMG_0003.CR2",
                            "jpeg": "PhotoManager/Processed/2024/IMG_0003_developed.jpg"}
    d = enable(env, mode="alongside")
    d = env["c"].get("/api/develop").json
    assert d["example"]["jpeg"] == "Pictures/Canon/2024/IMG_0003_developed.jpg"
    assert d["output_mode"] == "alongside"


def test_folder_overlap_rules_depend_on_the_output_mode(env):
    env["photos"].mkdir(parents=True)
    choose_photos(env)
    c = env["c"]
    # in "separate" mode the developed folder can't sit inside the photo folder
    r = c.post("/api/folders", json={"processed": "Pictures/Canon/Out"})
    assert r.status_code == 400
    # in "alongside" mode it isn't used, so it may be anything…
    enable(env, mode="alongside")
    assert c.post("/api/folders", json={"processed": "Pictures/Canon/Out"}).status_code == 200
    # …but switching back to "separate" is refused until it's fixed
    r = c.post("/api/develop/settings", json={"output_mode": "separate"})
    assert r.status_code == 400 and "can't be the same or inside each other" in r.json["error"]
    assert c.get("/api/develop").json["output_mode"] == "alongside"
    assert c.post("/api/develop/settings", json={"output_mode": "elsewhere"}).status_code == 400
    # the quarantine folder is checked in both modes
    r = c.post("/api/folders", json={"quarantine": "Pictures/Canon"})
    assert r.status_code == 400


# --- safety ------------------------------------------------------------------------------------------
def test_an_existing_jpeg_is_never_overwritten(env):
    library(env)
    mine = env["nas"] / "PhotoManager" / "Processed" / "2023" / "IMG_0001_developed.jpg"
    put(mine, b"my own file")
    enable(env)
    st = develop_now(env)
    assert st["done"] == 3 and st["existing"] == 1
    assert mine.read_bytes() == b"my own file"
    assert len(stub_calls(env)) == 2


def test_failures_are_recorded_retried_three_times_then_left(env):
    library(env, extra=[("2024/IMG_0004.CR2", build_cr2(taken="2024:09:01 10:00:00", pad=44) + b"FAILME")])
    enable(env)
    st = develop_now(env)
    assert st["done"] == 3 and st["failed"] == 1
    out = env["nas"] / "PhotoManager" / "Processed" / "2024"
    assert not (out / "IMG_0004_developed.jpg").exists()
    f = env["c"].get("/api/develop").json
    assert f["counts"]["failed"] == 1 and f["counts"]["developed"] == 3
    assert f["failed"][0]["rel"] == "2024/IMG_0004.CR2" and f["failed"][0]["attempts"] == 1
    assert "couldn't develop" in f["failed"][0]["error"] and "cannot decode" in f["failed"][0]["error"]
    assert f["failed"][0]["will_retry"] is True
    assert not [p for p in env["nas"].rglob("*partial*")]
    assert develop_now(env)["failed"] == 1              # attempt 2
    assert develop_now(env)["failed"] == 1              # attempt 3
    assert env["c"].get("/api/develop").json["counts"]["gave_up"] == 1
    n = len(stub_calls(env))
    assert develop_now(env)["total"] == 0 and len(stub_calls(env)) == n     # gave up
    assert env["c"].post("/api/develop/retry-failed").json == {"reset": 1}
    assert develop_now(env)["failed"] == 1 and len(stub_calls(env)) == n + 1


@pytest.mark.parametrize("marker, text", [(b"NOOUT", "couldn't develop"), (b"BADJPEG", "damaged JPEG")])
def test_bad_darktable_output_is_never_saved(env, marker, text):
    library(env, extra=[("2024/IMG_0004.CR2", build_cr2(pad=55) + marker)])
    enable(env)
    st = develop_now(env)
    assert st["failed"] == 1 and st["done"] == 3
    assert text in env["c"].get("/api/develop").json["failed"][0]["error"]
    assert not (env["nas"] / "PhotoManager" / "Processed" / "2024" / "IMG_0004_developed.jpg").exists()


def test_a_file_changed_since_the_scan_is_not_developed(env, monkeypatch):
    library(env)
    enable(env)
    monkeypatch.setattr(env["proc"], "_scan_first", lambda: None)   # keep the old scan result
    with open(env["photos"] / "2024" / "IMG_0003.CR2", "ab") as fh:
        fh.write(b"more")
    st = develop_now(env)
    assert st["done"] == 2 and st["failed"] == 1
    assert "changed since the last scan" in env["c"].get("/api/develop").json["failed"][0]["error"]


def test_slow_darktable_times_out(env):
    env["cfg"].dt_timeout = 1
    library(env, extra=[("2024/IMG_0004.CR2", build_cr2(pad=66) + b"SLOW")])
    enable(env)
    t0 = time.time()
    st = develop_now(env)
    assert time.time() - t0 < 15
    assert st["failed"] == 1 and "took longer" in env["c"].get("/api/develop").json["failed"][0]["error"]


def test_identical_copies_are_developed_once(env):
    same = build_cr2(taken="2023:01:01 10:00:00", pad=77)
    library(env, extra=[("a/IMG_9.CR2", same), ("b/IMG_9 copy.CR2", same)])
    run_scan(env)
    enable(env)
    st = develop_now(env)
    assert st["done"] == 4 and len(stub_calls(env)) == 4       # 3 originals + ONE of the pair
    c = env["c"].get("/api/develop").json["counts"]
    assert c["pending"] == 0 and c["duplicates_skipped"] == 1
    out = env["nas"] / "PhotoManager" / "Processed"
    assert (out / "a" / "IMG_9_developed.jpg").exists() and not (out / "b").exists()


def test_quarantining_the_developed_copy_does_not_develop_the_other_again(env):
    same = build_cr2(taken="2023:01:01 10:00:00", pad=77)
    library(env, extra=[("a/IMG_9.CR2", same), ("b/IMG_9 copy.CR2", same)])
    run_scan(env)
    enable(env)
    develop_now(env)
    n = len(stub_calls(env))
    r = env["c"].post("/api/quarantine/move", json={"keep": "b/IMG_9 copy.CR2", "move": ["a/IMG_9.CR2"]})
    assert r.status_code == 200, r.json
    st = develop_now(env)
    assert st["total"] == 0 and len(stub_calls(env)) == n


# --- the overnight window ------------------------------------------------------------------------------
@pytest.mark.parametrize("window, hour, expected", [
    ((1, 6), 0, None), ((1, 6), 1, "2026-10-21"), ((1, 6), 5, "2026-10-21"), ((1, 6), 6, None),
    ((22, 6), 21, None), ((22, 6), 22, "2026-10-21"), ((22, 6), 23, "2026-10-21"),
    ((22, 6), 0, "2026-10-20"), ((22, 6), 5, "2026-10-20"), ((22, 6), 6, None),
    ((3, 3), 0, "2026-10-21"), ((3, 3), 15, "2026-10-21"),
])
def test_window_ids(env, window, hour, expected):
    env["cfg"].options["develop_start_hour"], env["cfg"].options["develop_end_hour"] = window
    assert env["proc"].window_id(datetime(2026, 10, 21, hour, 30)) == expected


def test_scheduled_run_happens_once_per_night(env):
    library(env)
    enable(env)
    proc = env["proc"]
    proc.clock = lambda: OUT_OF_WINDOW
    assert proc.scheduled_tick() is False and not stub_calls(env)
    proc.clock = lambda: IN_WINDOW
    assert proc.scheduled_tick() is True
    st = wait_develop(env)
    assert st["trigger"] == "schedule" and st["done"] == 3 and not st["stop_reason"]
    assert proc.scheduled_tick() is False                       # tonight's run is done
    put(env["photos"] / "2024" / "IMG_0005.CR2", build_cr2(taken="2024:10:01 10:00:00", pad=88))
    assert proc.scheduled_tick() is False                       # new photo waits until tomorrow
    proc.clock = lambda: datetime(2026, 10, 22, 2, 0)
    assert proc.scheduled_tick() is True
    st = wait_develop(env)
    assert st["done"] == 1                                       # the run scanned first and found it
    assert (env["nas"] / "PhotoManager" / "Processed" / "2024" / "IMG_0005_developed.jpg").exists()


def test_a_scheduled_run_pauses_at_the_end_of_the_window_and_continues_next_night(env, monkeypatch):
    library(env)
    enable(env)
    proc = env["proc"]
    proc.clock = lambda: IN_WINDOW
    real = proc.in_window

    def closing():
        return len(stub_calls(env)) < 1     # open until the first photo has been developed
    monkeypatch.setattr(proc, "in_window", closing)
    assert proc.scheduled_tick() is True
    st = wait_develop(env)
    assert st["done"] == 1 and st["stop_reason"] == "window" and "next night" in st["stop_text"]
    monkeypatch.setattr(proc, "in_window", real)
    assert proc.scheduled_tick() is True                        # not marked done: carries on
    st = wait_develop(env)
    assert st["done"] == 2 and not st["stop_reason"]


def test_a_manual_run_ignores_the_window(env):
    library(env)
    enable(env)
    env["proc"].clock = lambda: OUT_OF_WINDOW
    assert develop_now(env)["done"] == 3


# --- stopping ---------------------------------------------------------------------------------------------
def test_low_space_stops_the_run_without_failures(env, monkeypatch):
    library(env)
    enable(env)
    monkeypatch.setattr(processor_mod, "disk", lambda p: {"free_gb": 0.5})
    st = develop_now(env)
    assert st["stop_reason"] == "low_space" and st["done"] == 0 and st["failed"] == 0
    assert not stub_calls(env)
    assert env["c"].get("/api/develop").json["counts"]["failed"] == 0


def test_turning_the_switch_off_stops_a_scheduled_run_but_not_a_manual_one(env, monkeypatch):
    library(env)
    enable(env)
    real = env["proc"]._develop

    def develop_then_switch_off(rel, size):
        res = real(rel, size)
        env["c"].post("/api/develop/settings", json={"enabled": False})
        return res
    monkeypatch.setattr(env["proc"], "_develop", develop_then_switch_off)
    st = develop_now(env)                              # manual: finishes everything
    assert st["done"] == 3 and not st["stop_reason"]
    enable(env)
    env["proc"].clock = lambda: IN_WINDOW
    put(env["photos"] / "2025" / "IMG_0009.CR2", build_cr2(taken="2025:01:01 10:00:00", pad=5))
    put(env["photos"] / "2025" / "IMG_0010.CR2", build_cr2(taken="2025:01:02 10:00:00", pad=6))
    assert env["proc"].scheduled_tick() is True        # scheduled: stops after the first photo
    st = wait_develop(env)
    assert st["done"] == 1 and st["stop_reason"] == "disabled"


def test_cancel_kills_darktable_and_cleans_up(env):
    library(env, extra=[("2024/IMG_0004.CR2", build_cr2(taken="2025:01:01 10:00:00", pad=99) + b"SLOW")])
    enable(env)
    assert env["c"].post("/api/develop/run").status_code == 200
    end = time.time() + 10
    while time.time() < end and not stub_calls(env):
        time.sleep(0.05)
    assert stub_calls(env), "darktable never started"
    assert env["c"].post("/api/scan").status_code == 409        # can't scan while developing
    assert env["c"].post("/api/develop/run").status_code == 409  # or start a second run
    t0 = time.time()
    env["c"].post("/api/develop/cancel")
    st = wait_develop(env, timeout=10)
    assert time.time() - t0 < 5
    assert st["stop_reason"] == "cancelled" and st["done"] == 0 and st["failed"] == 0
    assert not (env["cfg"].tmp_dir / "work").exists()
    assert env["c"].get("/api/develop").json["counts"]["failed"] == 0


def test_last_run_survives_a_restart(env):
    library(env)
    enable(env)
    develop_now(env)
    svc2 = Services(env["cfg"], mounts_file=str(env["mounts"]))
    st = svc2.processor.state
    assert st["done"] == 3 and st["finished"] and not st["running"]


# --- Home Assistant ---------------------------------------------------------------------------------------
def test_home_assistant_gets_develop_and_quarantine_entities(env):
    posted, events = {}, []
    ha = env["svc"].ha
    ha.enabled = True
    ha._post = lambda path, body: (events.append((path, body)) if "/events/" in path
                                   else posted.__setitem__(path.rsplit("/", 1)[-1].split(".", 1)[-1], body))
    library(env)
    env["svc"].publish()
    assert posted["photo_manager_developed"]["state"] == 0
    assert posted["photo_manager_developed"]["attributes"]["enabled"] is False
    assert posted["photo_manager_developed"]["attributes"]["to_develop"] == 3
    assert posted["photo_manager_developed"]["attributes"]["window"] == "01:00–06:00"
    assert posted["photo_manager_quarantine"]["state"] == 0
    enable(env)
    develop_now(env)
    deadline = time.time() + 5
    while time.time() < deadline and not events:
        time.sleep(0.05)
    assert events and events[-1][0].endswith("/events/photo_manager_develop_finished")
    assert events[-1][1]["developed"] == 3 and events[-1][1]["failed"] == 0
    env["svc"].publish()
    assert posted["photo_manager_developed"]["state"] == 3
    assert posted["photo_manager_status"]["state"] == "idle"


# --- test one folder only / skip RAWs that already have a JPEG ----------------------------------------
def test_develop_only_the_chosen_folder(env):
    library(env)
    r = env["c"].post("/api/develop/settings", json={"scope": "2023"})
    assert r.status_code == 200 and r.json["scope"] == "2023"
    assert env["c"].get("/api/develop").json["counts"]["pending"] == 2
    enable(env)
    st = develop_now(env)
    assert st["done"] == 2 and st["total"] == 2
    out = env["nas"] / "PhotoManager" / "Processed"
    assert sorted(p.name for p in out.rglob("*.jpg")) == ["IMG_0001_developed.jpg",
                                                          "IMG_0002_developed.jpg"]
    assert not (out / "2024").exists()
    # widening the scope picks up the rest, without redoing the first two
    env["c"].post("/api/develop/settings", json={"scope": ""})
    assert develop_now(env)["done"] == 1 and len(stub_calls(env)) == 3


def test_scope_matches_whole_folder_names_only_and_is_validated(env):
    library(env, extra=[("2023-extra/IMG_7.CR2", build_cr2(pad=7))])
    run_scan(env)
    env["c"].post("/api/develop/settings", json={"scope": "2023"})
    assert env["c"].get("/api/develop").json["counts"]["pending"] == 2      # not "2023-extra"
    for bad in ("../Other", "nope", "2023/IMG_0001.CR2", 5):
        r = env["c"].post("/api/develop/settings", json={"scope": bad})
        assert r.status_code == 400, bad
    assert env["c"].get("/api/develop").json["scope"] == "2023"
    # persists across a restart, and changing the photo folder clears it
    assert Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["develop_scope"] == "2023"
    env["photos"].parent.joinpath("Other").mkdir()
    assert env["c"].post("/api/folders", json={"photo": "Pictures/Other"}).status_code == 200
    assert env["c"].get("/api/develop").json["scope"] == ""


def test_scope_applies_to_scheduled_runs_too(env):
    library(env)
    env["c"].post("/api/develop/settings", json={"scope": "2024"})
    enable(env)
    env["proc"].clock = lambda: IN_WINDOW
    assert env["proc"].scheduled_tick() is True
    st = wait_develop(env)
    assert st["done"] == 1 and len(stub_calls(env)) == 1


def test_skip_raws_that_already_have_a_jpeg(env):
    put(env["photos"] / "2023" / "IMG_0001.JPG", b"camera jpeg")
    library(env)
    d = env["c"].get("/api/develop").json
    assert d["skip_with_jpeg"] is False and d["counts"]["pending"] == 3     # off by default
    r = env["c"].post("/api/develop/settings", json={"skip_with_jpeg": True})
    assert r.status_code == 200 and r.json["skip_with_jpeg"] is True
    d = env["c"].get("/api/develop").json
    assert d["counts"]["pending"] == 2 and d["counts"]["with_camera_jpeg"] == 1
    enable(env)
    assert develop_now(env)["done"] == 2
    out = env["nas"] / "PhotoManager" / "Processed"
    assert not (out / "2023" / "IMG_0001_developed.jpg").exists()
    assert (env["photos"] / "2023" / "IMG_0001.JPG").read_bytes() == b"camera jpeg"
    # turning it off again develops the one that was left
    env["c"].post("/api/develop/settings", json={"skip_with_jpeg": False})
    assert develop_now(env)["done"] == 1
    assert env["c"].post("/api/develop/settings", json={"skip_with_jpeg": "yes"}).status_code == 400


def test_new_jpegs_never_overwrite_in_either_mode(env):
    library(env)
    # next to the RAW: a file already called IMG_0001_developed.jpg stays exactly as it is
    mine = put(env["photos"] / "2023" / "IMG_0001_developed.jpg", b"keep me")
    enable(env, mode="alongside")
    st = develop_now(env)
    assert st["done"] == 3 and st["existing"] == 1
    assert mine.read_bytes() == b"keep me" and len(stub_calls(env)) == 2
