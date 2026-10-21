import io
import json
import os
import stat
import struct
import time
import urllib.parse

import pytest
from PIL import Image

from helpers import build_cr2, choose_photos, put, run_scan
from photomanager import gphotos, thumbs
from test_develop import develop_now, enable, stub_calls, wait_develop
from test_quarantine import move, qdir, setup_dups


def jpeg(w=800, h=400, color=(200, 30, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


def cr2_with_preview(preview: bytes, orientation=1, taken="2024:05:01 10:00:00") -> bytes:
    """A TIFF laid out like a CR2: IFD0 carries the JPEG preview (strip) and the orientation."""
    make, model, tk = b"Canon\0", b"Canon EOS 5D Mark III\0", taken.encode() + b"\0"
    n = 7
    ifd0 = 8
    data = ifd0 + 2 + n * 12 + 4
    off_make = data
    off_model = off_make + len(make)
    off_exif = off_model + len(model)
    off_taken = off_exif + 2 + 12 + 4
    off_jpeg = off_taken + len(tk)
    ent = [(0x0103, 3, 1, 6), (0x010F, 2, len(make), off_make), (0x0110, 2, len(model), off_model),
           (0x0111, 4, 1, off_jpeg), (0x0112, 3, 1, orientation), (0x0117, 4, 1, len(preview)),
           (0x8769, 4, 1, off_exif)]
    out = b"II*\x00" + struct.pack("<IH", ifd0, n)
    for tag, typ, cnt, val in ent:
        out += struct.pack("<HHII", tag, typ, cnt, val)
    out += struct.pack("<I", 0) + make + model
    out += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(tk), off_taken) + struct.pack("<I", 0)
    return out + tk + preview


# --- thumbnails -----------------------------------------------------------------------------------
def test_preview_is_read_from_inside_the_cr2_and_shrunk(tmp_path):
    p = tmp_path / "a.CR2"
    p.write_bytes(cr2_with_preview(jpeg(1600, 800)))
    data, orient = thumbs.embedded_preview(p)
    assert data[:2] == b"\xff\xd8" and orient == 1
    img = Image.open(io.BytesIO(thumbs.make_thumbnail(p)))
    assert max(img.size) == thumbs.SIZE and img.size == (480, 240)


def test_thumbnail_is_rotated_by_the_orientation_tag(tmp_path):
    p = tmp_path / "a.CR2"
    p.write_bytes(cr2_with_preview(jpeg(800, 400), orientation=6))
    w, h = Image.open(io.BytesIO(thumbs.make_thumbnail(p))).size
    assert h > w


@pytest.mark.parametrize("data", [b"", b"II*\x00", b"not a photo at all", build_cr2()])
def test_files_without_a_preview_give_nothing(tmp_path, data):
    p = tmp_path / "a.CR2"
    p.write_bytes(data)
    assert thumbs.make_thumbnail(p) is None


def test_a_damaged_preview_gives_nothing(tmp_path):
    p = tmp_path / "a.CR2"
    p.write_bytes(cr2_with_preview(jpeg()[:-40]))
    assert thumbs.make_thumbnail(p) is None


def test_thumbnail_endpoint_serves_and_caches_library_photos(env, monkeypatch):
    put(env["photos"] / "a" / "IMG_1.CR2", cr2_with_preview(jpeg()))
    put(env["photos"] / "a" / "IMG_2.CR2", build_cr2())          # no preview inside
    choose_photos(env)
    run_scan(env)
    c = env["c"]
    r = c.get("/api/thumb?rel=a/IMG_1.CR2")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data[:2] == b"\xff\xd8"
    assert "private" in r.headers["Cache-Control"]
    cached = list((env["cfg"].data_dir / "thumbs").glob("*.jpg"))
    assert len(cached) == 1
    # the second request is answered from the cache, without opening the NAS file again
    monkeypatch.setattr(thumbs, "make_thumbnail", lambda p: (_ for _ in ()).throw(AssertionError("re-read")))
    assert c.get("/api/thumb?rel=a/IMG_1.CR2").data == r.data
    monkeypatch.undo()
    assert c.get("/api/thumb?rel=a/IMG_2.CR2").status_code == 404
    for bad in ("nope.CR2", "../../etc/passwd", "/etc/passwd", "", "a"):
        assert c.get("/api/thumb", query_string={"rel": bad}).status_code == 404, bad
    assert c.get("/api/thumb?q=999").status_code == 404
    assert c.get("/api/thumb?q=abc").status_code == 404


def test_thumbnails_of_quarantined_photos(env):
    same = cr2_with_preview(jpeg())
    setup_dups(env, copies=("a/IMG_1.CR2", "b/IMG_1 copy.CR2"), data=same)
    assert env["c"].get("/api/thumb?rel=b/IMG_1 copy.CR2").status_code == 200
    assert move(env, "a/IMG_1.CR2", ["b/IMG_1 copy.CR2"]).status_code == 200
    # gone from the library list, but visible in the quarantine list
    assert env["c"].get("/api/thumb?rel=b/IMG_1 copy.CR2").status_code == 404
    item = env["c"].get("/api/quarantine").json["items"][0]
    r = env["c"].get(f"/api/thumb?q={item['id']}")
    assert r.status_code == 200 and r.data[:2] == b"\xff\xd8"
    (qdir(env) / "b" / "IMG_1 copy.CR2").unlink()
    env["c"].post("/api/quarantine/forget-missing")
    assert env["c"].get(f"/api/thumb?q={item['id']}").status_code == 404


# --- Google Photos --------------------------------------------------------------------------------
class FakeGoogle:
    def __init__(self):
        self.calls, self.uploads, self.n = [], [], 0
        self.refresh_ok, self.scope = True, gphotos.SCOPE
        self.create_override = None     # callable(json) -> (status, dict)
        self.upload_status = 200
        self.expire_first_token = False

    def __call__(self, method, url, headers=None, data=None, timeout=300):
        self.calls.append((method, url, dict(headers or {}), data))
        if url == gphotos.TOKEN_URL:
            q = urllib.parse.parse_qs(data.decode())
            if q["grant_type"] == ["authorization_code"]:
                return 200, json.dumps({"access_token": "AT1", "refresh_token": "RT", "expires_in": 3600,
                                        "scope": self.scope}).encode()
            if not self.refresh_ok:
                return 400, b'{"error": "invalid_grant"}'
            return 200, json.dumps({"access_token": "AT2", "expires_in": 3600}).encode()
        if url == gphotos.UPLOAD_URL:
            if self.upload_status != 200:
                return self.upload_status, b'{"error": {"message": "nope"}}'
            if self.expire_first_token and headers["Authorization"] == "Bearer AT1":
                return 401, b"{}"
            self.uploads.append(data)
            return 200, f"TOKEN{len(self.uploads)}".encode()
        if url == gphotos.CREATE_URL:
            body = json.loads(data)
            if self.create_override:
                status, res = self.create_override(body)
                return status, json.dumps(res).encode()
            self.n += 1
            return 200, json.dumps({"newMediaItemResults": [{
                "uploadToken": body["newMediaItems"][0]["simpleMediaItem"]["uploadToken"],
                "status": {"message": "Success"}, "mediaItem": {"id": f"MEDIA{self.n}"}}]}).encode()
        raise AssertionError(url)


@pytest.fixture
def g(env, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gphotos, "http", fake)
    env["cfg"].options["google_client_id"] = "cid"
    env["cfg"].options["google_client_secret"] = "secret"
    return fake


def connect(env):
    url = env["c"].post("/api/google/auth-url").json["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    r = env["c"].post("/api/google/connect", json={
        "redirect_url": f"http://localhost/?state={state}&code=4%2Fabc&scope=x"})
    assert r.status_code == 200, r.json
    return r.json


def library3(env):
    for name, pad in (("2023/IMG_0001", 11), ("2023/IMG_0002", 22), ("2024/IMG_0003", 33)):
        put(env["photos"] / f"{name}.CR2", build_cr2(taken="2023:01:01 10:00:00", pad=pad))
    choose_photos(env)
    run_scan(env)


def wait_upload(env, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not env["svc"].uploader.state["running"]:
            return env["svc"].uploader.state
        time.sleep(0.05)
    raise AssertionError("upload did not finish")


def test_google_needs_credentials_then_a_connection(env):
    d = env["c"].get("/api/google").json
    assert d["configured"] is False and d["connected"] is False and d["enabled"] is False
    r = env["c"].post("/api/google/auth-url")
    assert r.status_code == 400 and "client ID" in r.json["error"]
    assert env["c"].post("/api/google/settings", json={"upload": True}).status_code == 400
    assert env["c"].post("/api/google/run").status_code == 400


def test_sign_in_by_pasting_the_address_google_sends_you_to(env, g):
    url = env["c"].post("/api/google/auth-url").json["url"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith(gphotos.AUTH_URL)
    assert q["scope"] == [gphotos.SCOPE] and q["client_id"] == ["cid"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["http://localhost"]
    c = env["c"]
    for bad, text in (("", "Paste"), ("http://localhost/?error=access_denied&state=x", "access_denied"),
                      ("http://localhost/?state=WRONG&code=abc", "different sign-in"),
                      ("http://localhost/?state=" + q["state"][0], "doesn't contain")):
        r = c.post("/api/google/connect", json={"redirect_url": bad})
        assert r.status_code == 400 and text in r.json["error"], (bad, r.json)
    assert c.get("/api/google").json["connected"] is False
    d = connect(env)
    assert d["connected"] is True
    sent = urllib.parse.parse_qs(g.calls[-1][3].decode())
    assert sent["code"] == ["4/abc"] and sent["client_secret"] == ["secret"]
    f = env["cfg"].data_dir / "google.json"
    assert json.loads(f.read_text())["refresh_token"] == "RT"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    # a used link can't be used again
    assert c.post("/api/google/connect", json={"redirect_url": "http://localhost/?state=x&code=y"}).status_code == 400


def test_sign_in_is_refused_if_google_photos_permission_wasnt_granted(env, g):
    g.scope = "openid"
    url = env["c"].post("/api/google/auth-url").json["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    r = env["c"].post("/api/google/connect", json={"redirect_url": f"http://localhost/?state={state}&code=c"})
    assert r.status_code == 400 and "permission" in r.json["error"]
    assert not (env["cfg"].data_dir / "google.json").exists()


def test_upload_developed_jpegs_with_the_automatic_switch_off(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    d = env["c"].get("/api/google").json
    assert d["enabled"] is False and d["counts"]["pending"] == 3
    out = env["nas"] / "PhotoManager" / "Processed"
    before = sorted((str(p), p.read_bytes()) for p in out.rglob("*.jpg"))
    assert env["c"].post("/api/google/run").status_code == 200        # "Upload now", no switch needed
    st = wait_upload(env)
    assert st["done"] == 3 and st["failed"] == 0 and not st["stop_reason"]
    assert len(g.uploads) == 3 and all(u[:2] == b"\xff\xd8" for u in g.uploads)
    up = [c for c in g.calls if c[1] == gphotos.UPLOAD_URL][0]
    assert up[2]["Authorization"] == "Bearer AT1" and up[2]["X-Goog-Upload-Protocol"] == "raw"
    create = json.loads([c for c in g.calls if c[1] == gphotos.CREATE_URL][0][3])
    item = create["newMediaItems"][0]
    assert item["simpleMediaItem"]["fileName"].endswith("_developed.jpg") and "albumId" not in create
    d = env["c"].get("/api/google").json
    assert d["counts"] == {"pending": 0, "uploaded": 3, "failed": 0, "gave_up": 0}
    assert sorted((str(p), p.read_bytes()) for p in out.rglob("*.jpg")) == before    # nothing changed
    # never twice
    n = len(g.uploads)
    assert env["c"].post("/api/google/run").status_code == 200
    wait_upload(env)
    assert len(g.uploads) == n


def test_upload_runs_by_itself_after_developing_when_switched_on(env, g):
    library3(env)
    connect(env)
    assert env["c"].post("/api/google/settings", json={"upload": True}).json["enabled"] is True
    develop_now(env)
    end = time.time() + 10
    while time.time() < end and len(g.uploads) < 3:
        time.sleep(0.05)
    wait_upload(env)
    assert len(g.uploads) == 3
    st = env["svc"].uploader.state
    assert st["trigger"] == "auto"
    assert env["c"].post("/api/google/settings", json={"upload": "yes"}).status_code == 400


def test_the_scheduler_picks_up_waiting_jpegs_only_when_switched_on(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    up = env["svc"].uploader
    assert up.scheduled_tick(False) is False                 # switch off: nothing happens
    env["c"].post("/api/google/settings", json={"upload": True})
    assert up.scheduled_tick(True) is False                  # not while developing
    assert up.scheduled_tick(False) is True
    wait_upload(env)
    assert len(g.uploads) == 3
    assert up.scheduled_tick(False) is False                 # nothing left


def test_files_that_were_already_on_the_nas_are_never_uploaded(env, g):
    library3(env)
    put(env["nas"] / "PhotoManager" / "Processed" / "2023" / "IMG_0001_developed.jpg", jpeg())
    develop_now(env)
    connect(env)
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["total"] == 2 and len(g.uploads) == 2


def test_a_photo_google_rejects_is_recorded_and_retried(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    g.create_override = lambda body: (200, {"newMediaItemResults": [{"status": {"message": "Bad image"}}]})
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["failed"] == 3 and st["done"] == 0
    d = env["c"].get("/api/google").json
    assert d["counts"]["failed"] == 3 and d["failed"][0]["will_retry"] is True
    assert "Bad image" in d["failed"][0]["error"]
    g.create_override = None
    env["c"].post("/api/google/run")
    assert wait_upload(env)["done"] == 3
    assert env["c"].get("/api/google").json["counts"]["failed"] == 0


def test_failed_uploads_give_up_after_three_tries_until_retry_is_pressed(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    g.create_override = lambda body: (200, {"newMediaItemResults": [{"status": {"message": "x"}}]})
    for _ in range(3):
        env["c"].post("/api/google/run")
        wait_upload(env)
    assert env["c"].get("/api/google").json["counts"]["gave_up"] == 3
    n = len(g.uploads)
    env["c"].post("/api/google/run")
    assert wait_upload(env)["total"] == 0 and len(g.uploads) == n
    assert env["c"].post("/api/google/retry-failed").json == {"reset": 3}
    g.create_override = None
    env["c"].post("/api/google/run")
    assert wait_upload(env)["done"] == 3


def test_google_limits_pause_the_run_without_counting_failures(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    g.upload_status = 429
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["stop_reason"] == "rate" and st["failed"] == 0 and st["done"] == 0
    assert env["c"].get("/api/google").json["counts"] == {"pending": 3, "uploaded": 0, "failed": 0, "gave_up": 0}


def test_an_expired_access_token_is_refreshed_and_retried_once(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    g.expire_first_token = True
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["done"] == 3
    assert any(urllib.parse.parse_qs(c[3].decode()).get("grant_type") == ["refresh_token"]
               for c in g.calls if c[1] == gphotos.TOKEN_URL)


def test_a_revoked_sign_in_asks_you_to_reconnect(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    g.expire_first_token, g.refresh_ok = True, False
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["stop_reason"] == "auth" and st["done"] == 0 and st["failed"] == 0
    d = env["c"].get("/api/google").json
    assert d["needs_reconnect"] is True and d["counts"]["pending"] == 3


def test_a_missing_or_changed_jpeg_is_not_uploaded(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    out = env["nas"] / "PhotoManager" / "Processed"
    (out / "2023" / "IMG_0001_developed.jpg").unlink()
    with open(out / "2023" / "IMG_0002_developed.jpg", "ab") as fh:
        fh.write(b"x")
    env["c"].post("/api/google/run")
    st = wait_upload(env)
    assert st["done"] == 1 and st["failed"] == 2 and len(g.uploads) == 1
    errs = " ".join(f["error"] for f in env["c"].get("/api/google").json["failed"])
    assert "no longer on the NAS" in errs and "changed" in errs


def test_cancel_and_disconnect(env, g):
    library3(env)
    develop_now(env)
    connect(env)
    env["svc"].uploader._cancel = True
    env["svc"].uploader.state["running"] = False
    d = env["c"].post("/api/google/settings", json={"upload": True}).json
    assert d["enabled"] is True
    d = env["c"].post("/api/google/disconnect").json
    assert d["connected"] is False and d["enabled"] is False
    assert not (env["cfg"].data_dir / "google.json").exists()
    assert env["c"].post("/api/google/run").status_code == 400
    assert env["c"].post("/api/google/cancel").json == {"ok": True}


def test_changing_the_client_id_disconnects(env, g):
    connect(env)
    assert env["c"].get("/api/google").json["connected"] is True
    env["cfg"].options["google_client_id"] = "other"
    assert env["c"].get("/api/google").json["connected"] is False


def test_home_assistant_gets_the_google_photos_sensor(env, g):
    posted = {}
    ha = env["svc"].ha
    ha.enabled = True
    ha._post = lambda path, body: posted.__setitem__(path.rsplit("/", 1)[-1].split(".", 1)[-1], body)
    library3(env)
    develop_now(env)
    connect(env)
    env["c"].post("/api/google/run")
    wait_upload(env)
    env["svc"].publish()
    s = posted["photo_manager_google_photos"]
    assert s["state"] == 3 and s["attributes"]["connected"] is True and s["attributes"]["waiting"] == 0
