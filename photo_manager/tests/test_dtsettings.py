import time

import pytest

from helpers import build_cr2, choose_photos, put, run_scan
from photomanager import dtsettings
from photomanager.web import Services
from test_develop import develop_now, library, stub_calls


def args_of(call):
    a = call["args"]
    return a[a.index("--core") + 1:] if "--core" in a else a


def opt(call, flag):
    a = call["args"]
    return a[a.index(flag) + 1]


def test_defaults_match_a_normal_develop(env):
    library(env)
    d = env["c"].get("/api/develop").json
    assert d["dt"] == dtsettings.DEFAULT_DT and [p["id"] for p in d["dt_options"]["presets"]] == [
        "standard", "sigmoid", "classic", "minimal"]
    develop_now(env)
    c = stub_calls(env)[0]
    assert "plugins/darkroom/workflow=scene-referred (filmic)" in c["args"]
    assert "plugins/imageio/format/jpeg/quality=92" in c["args"]       # the add-on option still applies
    assert opt(c, "--icc-type") == "SRGB" and opt(c, "--hq") == "true"
    assert opt(c, "--width") == "0" and opt(c, "--height") == "0"      # full size
    assert c["xmp"] is None


def test_changed_settings_reach_darktable_and_persist(env):
    library(env)
    r = env["c"].post("/api/develop/dt", json={"preset": "sigmoid", "quality": 85, "max_size": 2048,
                                               "hq": False, "colour": "ADOBERGB",
                                               "intent": "RELATIVE_COLORIMETRIC"})
    assert r.status_code == 200 and r.json["dt"]["preset"] == "sigmoid"
    develop_now(env)
    c = stub_calls(env)[0]
    assert "plugins/darkroom/workflow=scene-referred (sigmoid)" in c["args"]
    assert "plugins/imageio/format/jpeg/quality=85" in c["args"]
    assert (opt(c, "--icc-type"), opt(c, "--icc-intent"), opt(c, "--hq")) == (
        "ADOBERGB", "RELATIVE_COLORIMETRIC", "false")
    assert (opt(c, "--width"), opt(c, "--height")) == ("2048", "2048")
    again = Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["dt"]
    assert again["quality"] == 85 and again["preset"] == "sigmoid"
    # one setting can be changed without losing the others
    env["c"].post("/api/develop/dt", json={"quality": 70})
    assert env["c"].get("/api/develop").json["dt"]["preset"] == "sigmoid"


@pytest.mark.parametrize("bad", [
    {"preset": "wild"}, {"quality": 10}, {"quality": 101}, {"quality": "90"}, {"quality": True},
    {"max_size": 100}, {"max_size": -5}, {"max_size": 99999}, {"hq": "yes"}, {"colour": "NONE"},
    {"intent": "x"}, {"xmp_template": "../x.xmp"}, {"xmp_template": "a.txt"}, {"xmp_template": 5},
    {"nonsense": 1}])
def test_bad_settings_are_refused_and_nothing_is_saved(env, bad):
    before = env["c"].get("/api/develop").json["dt"]
    r = env["c"].post("/api/develop/dt", json={"quality": 80, **bad} if "quality" not in bad else bad)
    assert r.status_code == 400 and r.json["error"]
    assert env["c"].get("/api/develop").json["dt"] == before


def test_quality_can_go_back_to_the_add_on_default(env):
    env["c"].post("/api/develop/dt", json={"quality": 60})
    env["c"].post("/api/develop/dt", json={"quality": None})
    library(env)
    develop_now(env)
    assert "plugins/imageio/format/jpeg/quality=92" in stub_calls(env)[0]["args"]


def test_damaged_saved_settings_fall_back_to_defaults(env):
    (env["cfg"].data_dir / "settings.json").write_text('{"dt": {"preset": "wild", "quality": 5, "hq": false}}')
    dt = Services(env["cfg"], mounts_file=str(env["mounts"])).storage.settings["dt"]
    assert dt["preset"] == "standard" and dt["quality"] is None and dt["hq"] is False


def test_xmp_template_is_copied_and_passed_to_darktable(env, tmp_path):
    tdir = tmp_path / "config"
    tdir.mkdir()
    (tdir / "my look.xmp").write_text("<x:xmpmeta>MYLOOK</x:xmpmeta>")
    (tdir / "notes.txt").write_text("ignored")
    env["cfg"].template_dir = tdir
    library(env)
    assert env["c"].get("/api/develop").json["templates"] == ["my look.xmp"]
    assert env["c"].post("/api/develop/dt", json={"xmp_template": "my look.xmp"}).status_code == 200
    develop_now(env)
    c = stub_calls(env)[0]
    assert c["xmp"] and c["xmp"].endswith("template.xmp") and "MYLOOK" in c["xmp_text"]
    assert str(env["cfg"].tmp_dir) in c["xmp"] and str(tdir) not in c["xmp"]    # a private copy
    assert (tdir / "my look.xmp").read_text() == "<x:xmpmeta>MYLOOK</x:xmpmeta>"
    assert c["args"][0] == c["src_dir"] + "/input.cr2" and c["args"][1] == c["xmp"]


def test_a_missing_template_fails_the_photo_clearly(env, tmp_path):
    env["cfg"].template_dir = tmp_path / "nowhere"
    env["c"].post("/api/develop/dt", json={"xmp_template": "gone.xmp"})
    library(env)
    st = develop_now(env)
    assert st["failed"] == 3 and not stub_calls(env)
    assert "isn't in the add-on's config folder" in env["c"].get("/api/develop").json["failed"][0]["error"]


# --- try it on one photo -------------------------------------------------------------------------------
def wait_preview(env, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = env["c"].get("/api/develop/preview").json
        if st and not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("preview did not finish")


def test_preview_uses_unsaved_settings_and_writes_nothing_to_the_nas(env):
    library(env)
    before = sorted(str(p) for p in env["nas"].rglob("*"))
    saved = env["c"].get("/api/develop").json["dt"]
    r = env["c"].post("/api/develop/preview", json={"rel": "2023/IMG_0001.CR2",
                                                    "dt": {"preset": "classic", "quality": 70}})
    assert r.status_code == 200
    st = wait_preview(env)
    assert st["error"] is None and st["rel"] == "2023/IMG_0001.CR2"
    img = env["c"].get("/api/develop/preview.jpg")
    assert img.status_code == 200 and img.data[:2] == b"\xff\xd8" and img.headers["Cache-Control"] == "no-store"
    c = stub_calls(env)[0]
    assert "plugins/darkroom/workflow=display-referred (legacy)" in c["args"]
    assert (opt(c, "--width"), opt(c, "--height")) == ("1400", "1400")           # small and quick
    assert str(env["cfg"].tmp_dir / "preview") in c["src_dir"]
    assert sorted(str(p) for p in env["nas"].rglob("*")) == before              # nothing saved to the NAS
    assert env["c"].get("/api/develop").json["dt"] == saved                      # settings unchanged
    assert env["c"].get("/api/develop").json["counts"]["developed"] == 0         # not recorded as developed


def test_preview_refusals_and_errors(env):
    c = env["c"]
    assert c.get("/api/develop/preview.jpg").status_code == 404
    library(env)
    assert c.post("/api/develop/preview", json={"rel": "nope.CR2"}).status_code == 400
    assert c.post("/api/develop/preview", json={"rel": "2023/IMG_0001.CR2", "dt": {"quality": 1}}).status_code == 400
    env["proc"].state["running"] = True
    r = c.post("/api/develop/preview", json={"rel": "2023/IMG_0001.CR2"})
    assert r.status_code == 409
    env["proc"].state["running"] = False
    put(env["photos"] / "2023" / "BAD.CR2", build_cr2(pad=3) + b"FAILME")
    run_scan(env)
    assert c.post("/api/develop/preview", json={"rel": "2023/BAD.CR2"}).status_code == 200
    st = wait_preview(env)
    assert "couldn't develop" in st["error"]
    assert c.get("/api/develop/preview.jpg").status_code == 404       # a failed preview shows no stale picture
