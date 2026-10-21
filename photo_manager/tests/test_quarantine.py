import errno
import os
import zlib

import pytest

from helpers import build_cr2, choose_photos, put, run_scan
from photomanager import quarantine as qmod
from photomanager.quarantine import crc32_file

SAME = build_cr2(taken="2023:01:01 10:00:00", pad=40)
OTHER = build_cr2(taken="2023:02:02 10:00:00", pad=41)


def files_only(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def setup_dups(env, copies=("a/IMG_1.CR2", "b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"), data=SAME):
    for rel in copies:
        put(env["photos"] / rel, data, mtime=1_600_000_000)
    choose_photos(env)
    run_scan(env)


def verify(env, keep, move):
    r = env["c"].post("/api/quarantine/verify", json={"keep": keep, "move": move})
    return r


def move(env, keep, move_):
    return env["c"].post("/api/quarantine/move", json={"keep": keep, "move": move_})


def qdir(env):
    return env["nas"] / "PhotoManager" / "Quarantine"


def dup_groups(env):
    return env["c"].get("/api/duplicates").json


# --- checksum -----------------------------------------------------------------------------------------
def test_crc32_matches_zlib_and_the_usual_format(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"hello")
    assert crc32_file(f) == "3610A686"
    big = os.urandom(qmod.CHUNK * 2 + 123)
    f.write_bytes(big)
    assert crc32_file(f) == f"{zlib.crc32(big) & 0xFFFFFFFF:08X}"
    f.write_bytes(b"")
    assert crc32_file(f) == "00000000"


# --- duplicate groups ---------------------------------------------------------------------------------
def test_groups_list_every_copy_and_page(env):
    setup_dups(env)
    put(env["photos"] / "d" / "X.CR2", OTHER)
    put(env["photos"] / "e" / "X.CR2", OTHER)
    run_scan(env)
    g = dup_groups(env)
    assert g["total"] == 2 and len(g["groups"]) == 2
    big = g["groups"][0]
    assert big["count"] == 3 and [f["rel"] for f in big["files"]] == [
        "a/IMG_1.CR2", "b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"]
    assert env["c"].get("/api/duplicates?offset=1&limit=1").json["groups"][0]["count"] == 2
    assert env["c"].get("/api/duplicates?offset=5").json["groups"] == []


# --- verifying ----------------------------------------------------------------------------------------
def test_verify_shows_matching_checksums(env):
    setup_dups(env)
    r = verify(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"])
    assert r.status_code == 200
    v = r.json
    want = f"{zlib.crc32(SAME) & 0xFFFFFFFF:08X}"
    assert v["all_match"] is True and v["keep"]["crc32"] == want
    assert [f["crc32"] for f in v["files"]] == [want, want]
    assert all(f["match"] and f["problem"] is None and f["size"] == len(SAME) for f in v["files"])


def test_a_different_file_with_the_same_size_and_date_is_caught(env):
    setup_dups(env)
    # corrupt one copy in place, keeping size and modified time identical to the scan
    p = env["photos"] / "b" / "IMG_1 copy.CR2"
    data = bytearray(SAME)
    data[-1] ^= 0xFF
    p.write_bytes(bytes(data))
    os.utime(p, (1_600_000_000, 1_600_000_000))
    v = verify(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"]).json
    assert v["all_match"] is False
    bad, good = v["files"]
    assert bad["match"] is False and bad["crc32"] != v["keep"]["crc32"]
    assert bad["problem"] == "Different from the copy that stays"
    assert good["match"] is True


def test_a_file_changed_since_the_scan_is_flagged(env):
    setup_dups(env)
    with open(env["photos"] / "c" / "IMG_1 again.CR2", "ab") as fh:
        fh.write(b"x")
    v = verify(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"]).json
    assert v["all_match"] is False
    assert v["files"][1]["problem"] == "Changed since the last scan — scan again"
    assert v["files"][1]["crc32"] is None and v["files"][0]["match"] is True
    # and the kept copy changing is flagged too
    with open(env["photos"] / "a" / "IMG_1.CR2", "ab") as fh:
        fh.write(b"y")
    v = verify(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).json
    assert v["all_match"] is False and v["keep"]["problem"].startswith("Changed since")


def test_a_missing_file_is_a_problem_not_a_crash(env):
    setup_dups(env)
    (env["photos"] / "b" / "IMG_1 copy.CR2").unlink()
    v = verify(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).json
    assert v["all_match"] is False and v["files"][0]["problem"]


# --- moving -------------------------------------------------------------------------------------------
def test_move_only_the_chosen_copies_and_keep_the_folder_layout(env):
    setup_dups(env)
    r = move(env, "a/IMG_1.CR2", ["c/IMG_1 again.CR2"])        # b is deliberately left alone
    assert r.status_code == 200, r.json
    res = r.json
    assert res["errors"] == [] and res["kept"] == "a/IMG_1.CR2"
    assert res["crc32"] == f"{zlib.crc32(SAME) & 0xFFFFFFFF:08X}"
    assert [m["rel"] for m in res["moved"]] == ["c/IMG_1 again.CR2"]
    assert files_only(qdir(env)) == ["c/IMG_1 again.CR2"]
    assert (qdir(env) / "c" / "IMG_1 again.CR2").read_bytes() == SAME
    assert (env["photos"] / "a" / "IMG_1.CR2").exists()
    assert (env["photos"] / "b" / "IMG_1 copy.CR2").exists()
    assert not (env["photos"] / "c" / "IMG_1 again.CR2").exists()
    # the library forgets the moved file, so the group shrinks (and disappears at one copy)
    g = dup_groups(env)
    assert g["groups"][0]["count"] == 2
    assert move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).status_code == 200
    assert dup_groups(env)["total"] == 0
    assert files_only(env["photos"]) == ["a/IMG_1.CR2"]
    # the scan counts agree after rescanning, and quarantine isn't scanned as photos
    assert run_scan(env)["total_files"] == 1


def test_move_is_refused_on_a_mismatch_even_if_verify_was_skipped(env):
    setup_dups(env)
    p = env["photos"] / "b" / "IMG_1 copy.CR2"
    data = bytearray(SAME)
    data[10] ^= 0xFF
    p.write_bytes(bytes(data))
    os.utime(p, (1_600_000_000, 1_600_000_000))
    r = move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"])
    assert r.status_code == 400 and "Nothing was moved" in r.json["error"]
    assert not qdir(env).exists() or files_only(qdir(env)) == []     # not even the matching one
    assert len(files_only(env["photos"])) == 3


@pytest.mark.parametrize("keep, mv, text", [
    ("a/IMG_1.CR2", [], "Tick at least one"),
    ("a/IMG_1.CR2", None, "Tick at least one"),
    ("a/IMG_1.CR2", ["a/IMG_1.CR2"], "can't also be moved"),
    ("a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "b/IMG_1 copy.CR2"], "twice"),
    ("", ["b/IMG_1 copy.CR2"], "has to stay"),
    ("a/IMG_1.CR2", ["nope/missing.CR2"], "isn't in the library"),
    ("a/IMG_1.CR2", ["../../etc/passwd"], "isn't valid"),
    ("a/IMG_1.CR2", ["/etc/passwd"], "isn't in the library"),
    ("a/IMG_1.CR2", ["d/X.CR2"], "aren't a duplicate group"),
])
def test_bad_requests_are_refused_and_nothing_moves(env, keep, mv, text):
    setup_dups(env)
    put(env["photos"] / "d" / "X.CR2", OTHER)
    run_scan(env)
    before = files_only(env["photos"])
    for call in (verify, move):
        r = call(env, keep, mv)
        assert r.status_code == 400 and text in r.json["error"], r.json
    assert files_only(env["photos"]) == before


def test_one_copy_must_always_stay(env):
    setup_dups(env, copies=("a/1.CR2", "b/1.CR2"))
    # moving both is only possible by naming one as "keep", which can't be moved
    assert move(env, "a/1.CR2", ["a/1.CR2", "b/1.CR2"]).status_code == 400
    assert move(env, "a/1.CR2", ["b/1.CR2"]).status_code == 200
    # a lone file is no longer in a group, so it can't be "kept" against anything
    assert move(env, "a/1.CR2", ["b/1.CR2"]).status_code == 400
    assert (env["photos"] / "a" / "1.CR2").exists()


def test_too_many_files_at_once_is_refused(env):
    setup_dups(env)
    r = move(env, "a/IMG_1.CR2", [f"x/{i}.CR2" for i in range(qmod.MAX_MOVE + 1)])
    assert r.status_code == 400 and "at most" in r.json["error"]


def test_name_clash_in_quarantine_never_overwrites(env):
    setup_dups(env)
    put(qdir(env) / "b" / "IMG_1 copy.CR2", b"already here")
    assert move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).status_code == 200
    assert (qdir(env) / "b" / "IMG_1 copy.CR2").read_bytes() == b"already here"
    assert (qdir(env) / "b" / "IMG_1 copy (2).CR2").read_bytes() == SAME


def test_move_needs_a_quarantine_folder(env):
    setup_dups(env)
    env["svc"].storage.settings["quarantine_folder"] = None
    r = move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"])
    assert r.status_code == 200 and r.json["moved"] == [] and r.json["errors"]
    assert "quarantine folder" in r.json["errors"][0]["error"]
    assert len(files_only(env["photos"])) == 3


def test_moves_are_blocked_while_a_scan_or_develop_runs(env, monkeypatch):
    setup_dups(env)
    env["svc"].scanner.state["running"] = True
    r = move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"])
    assert r.status_code == 409 and "scan is running" in r.json["error"]
    assert env["c"].post("/api/quarantine/restore", json={"id": 1}).status_code == 409
    env["svc"].scanner.state["running"] = False
    env["proc"].state["running"] = True
    r = move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"])
    assert r.status_code == 409 and "Developing is running" in r.json["error"]
    env["proc"].state["running"] = False
    assert len(files_only(env["photos"])) == 3


# --- different volumes ----------------------------------------------------------------------------------
def test_moving_across_volumes_copies_checks_then_removes(env, monkeypatch):
    setup_dups(env)
    real_rename = os.rename

    def exdev(src, dst):
        if str(env["photos"]) in str(src):
            raise OSError(errno.EXDEV, "cross-device")
        return real_rename(src, dst)
    monkeypatch.setattr(qmod.os, "rename", exdev)
    assert move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).status_code == 200
    assert (qdir(env) / "b" / "IMG_1 copy.CR2").read_bytes() == SAME
    assert not (env["photos"] / "b" / "IMG_1 copy.CR2").exists()


def test_a_bad_copy_across_volumes_keeps_the_original(env, monkeypatch):
    setup_dups(env)
    monkeypatch.setattr(qmod.os, "rename",
                        lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    real_copy = qmod.shutil.copy2

    def bad_copy(src, dst, **kw):
        real_copy(src, dst, **kw)
        with open(dst, "r+b") as fh:
            fh.write(b"\x00\x00\x00\x00")
        return dst
    monkeypatch.setattr(qmod.shutil, "copy2", bad_copy)
    r = move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"])
    assert r.status_code == 200 and r.json["moved"] == []
    assert "didn't match" in r.json["errors"][0]["error"]
    assert (env["photos"] / "b" / "IMG_1 copy.CR2").read_bytes() == SAME      # still there
    assert not (qdir(env) / "b" / "IMG_1 copy.CR2").exists()                  # bad copy removed
    assert dup_groups(env)["groups"][0]["count"] == 3                         # library unchanged


# --- the quarantine list, restore ------------------------------------------------------------------------
def test_listing_restore_and_forget(env):
    setup_dups(env)
    assert env["c"].get("/api/quarantine").json["items"] == []
    move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2", "c/IMG_1 again.CR2"])
    lst = env["c"].get("/api/quarantine").json
    assert lst["folder"] == "PhotoManager/Quarantine" and lst["files"] == 2
    assert lst["bytes"] == 2 * len(SAME)
    item = next(i for i in lst["items"] if i["rel"] == "b/IMG_1 copy.CR2")
    assert item["present"] and item["keep_rel"] == "a/IMG_1.CR2"
    assert item["crc32"] == f"{zlib.crc32(SAME) & 0xFFFFFFFF:08X}"

    r = env["c"].post("/api/quarantine/restore", json={"id": item["id"]})
    assert r.status_code == 200 and r.json == {"restored": "b/IMG_1 copy.CR2"}
    assert (env["photos"] / "b" / "IMG_1 copy.CR2").read_bytes() == SAME
    assert dup_groups(env)["groups"] == [] or dup_groups(env)["groups"][0]["count"] >= 1
    assert env["c"].get("/api/quarantine").json["files"] == 1
    assert env["c"].post("/api/quarantine/restore", json={"id": item["id"]}).status_code == 400

    # the user empties the quarantine folder by hand
    (qdir(env) / "c" / "IMG_1 again.CR2").unlink()
    lst = env["c"].get("/api/quarantine").json
    assert lst["files"] == 0 and lst["items"][0]["present"] is False
    r = env["c"].post("/api/quarantine/restore", json={"id": lst["items"][0]["id"]})
    assert r.status_code == 400 and "no longer in the quarantine folder" in r.json["error"]
    assert env["c"].post("/api/quarantine/forget-missing").json == {"forgotten": 1}
    assert env["c"].get("/api/quarantine").json["items"] == []


def test_restore_never_overwrites_a_file_that_took_its_place(env):
    setup_dups(env)
    move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"])
    put(env["photos"] / "b" / "IMG_1 copy.CR2", b"someone's new file")
    item = env["c"].get("/api/quarantine").json["items"][0]
    r = env["c"].post("/api/quarantine/restore", json={"id": item["id"]})
    assert r.status_code == 400 and "already in the photo folder" in r.json["error"]
    assert (env["photos"] / "b" / "IMG_1 copy.CR2").read_bytes() == b"someone's new file"
    assert (qdir(env) / "b" / "IMG_1 copy.CR2").read_bytes() == SAME
    assert env["c"].post("/api/quarantine/restore", json={"id": "x"}).status_code == 400
    assert env["c"].post("/api/quarantine/restore", json={}).status_code == 400
