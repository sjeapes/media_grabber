import io
import sqlite3
import time

from PIL import Image

from helpers import build_cr2, choose_photos, put, run_scan
from test_develop import develop_now, library
from test_thumbs_google import cr2_with_preview, jpeg


def g(env, **q):
    r = env["c"].get("/api/gallery", query_string=q)
    assert r.status_code == 200, r.json
    return r.json


def rels(res):
    return [i["rel"] for i in res["items"]]


def test_lists_newest_first_with_paging(env):
    library(env)
    res = g(env)
    assert res["total"] == 3 and rels(res) == ["2024/IMG_0003.CR2", "2023/IMG_0002.CR2", "2023/IMG_0001.CR2"]
    assert res["counts"] == {"all": 3, "developed": 0, "undeveloped": 3, "recent": 0, "days": 7}
    assert rels(g(env, limit=2)) == ["2024/IMG_0003.CR2", "2023/IMG_0002.CR2"]
    assert rels(g(env, limit=2, offset=2)) == ["2023/IMG_0001.CR2"]
    assert g(env, offset=50)["items"] == []
    assert g(env, filter="bogus")["filter"] == "all"
    it = res["items"][0]
    assert it["developed"] is False and it["uploaded"] is False and it["camera"] == "Canon EOS 5D Mark III"
    assert env["c"].get("/api/gallery?limit=x&offset=-5").json["total"] == 3     # junk falls back to defaults


def test_filters_by_developed_state_and_recency(env):
    library(env)
    env["c"].post("/api/develop/dt", json={"quality": 90})
    env["c"].post("/api/develop/settings", json={"scope": "2023"})
    develop_now(env)
    assert rels(g(env, filter="developed")) == ["2023/IMG_0002.CR2", "2023/IMG_0001.CR2"]
    assert rels(g(env, filter="undeveloped")) == ["2024/IMG_0003.CR2"]
    res = g(env, filter="recent")
    assert res["total"] == 2 and all(i["developed"] and i["developed_at"] for i in res["items"])
    assert res["counts"]["recent"] == 2 and res["counts"]["undeveloped"] == 1
    # age one of them: the recent list shrinks, the developed list doesn't
    with sqlite3.connect(env["cfg"].db_path) as c:
        c.execute("UPDATE developed SET at=? WHERE rel='2023/IMG_0001.CR2'", (time.time() - 10 * 86400,))
    assert rels(g(env, filter="recent")) == ["2023/IMG_0002.CR2"]
    assert rels(g(env, filter="recent", days=30)) == ["2023/IMG_0002.CR2", "2023/IMG_0001.CR2"] or \
        sorted(rels(g(env, filter="recent", days=30))) == ["2023/IMG_0001.CR2", "2023/IMG_0002.CR2"]
    assert g(env, filter="developed")["total"] == 2


def test_recent_list_is_most_recently_developed_first(env):
    library(env)
    develop_now(env)
    with sqlite3.connect(env["cfg"].db_path) as c:
        for i, rel in enumerate(("2023/IMG_0001.CR2", "2024/IMG_0003.CR2", "2023/IMG_0002.CR2")):
            c.execute("UPDATE developed SET at=? WHERE rel=?", (time.time() - 100 + i, rel))
    assert rels(g(env, filter="recent")) == ["2023/IMG_0002.CR2", "2024/IMG_0003.CR2", "2023/IMG_0001.CR2"]


def test_folder_and_search_filters(env):
    library(env, extra=[("odd_name%/IMG_9.CR2", build_cr2(pad=9)), ("oddXname/IMG_8.CR2", build_cr2(pad=8)),
                        ("2023-extra/IMG_7.CR2", build_cr2(pad=7))])
    run_scan(env)
    assert sorted(rels(g(env, folder="2023"))) == ["2023/IMG_0001.CR2", "2023/IMG_0002.CR2"]   # not 2023-extra
    assert rels(g(env, folder="odd_name%")) == ["odd_name%/IMG_9.CR2"]                       # no wildcards
    assert rels(g(env, q="img_0003")) == ["2024/IMG_0003.CR2"]
    assert g(env, q="zzz")["total"] == 0 and g(env, folder="2023", q="0002")["total"] == 1


def test_uploaded_flag_and_counts(env):
    library(env)
    develop_now(env)
    with sqlite3.connect(env["cfg"].db_path) as c:
        c.execute("INSERT INTO uploaded (out_rel, raw_rel, status, attempts, at) "
                  "SELECT out_rel, rel, 'done', 1, 0 FROM developed WHERE rel='2024/IMG_0003.CR2'")
    items = {i["rel"]: i for i in g(env)["items"]}
    assert items["2024/IMG_0003.CR2"]["uploaded"] is True and items["2023/IMG_0001.CR2"]["uploaded"] is False


def test_thumbnails_of_developed_jpegs_and_large_sizes(env):
    put(env["photos"] / "a" / "IMG_1.CR2", cr2_with_preview(jpeg(2400, 1600)))
    choose_photos(env)
    run_scan(env)
    c = env["c"]
    assert c.get("/api/thumb?dev=a/IMG_1.CR2").status_code == 404                  # not developed yet
    small = Image.open(io.BytesIO(c.get("/api/thumb?rel=a/IMG_1.CR2").data)).size
    large = Image.open(io.BytesIO(c.get("/api/thumb?rel=a/IMG_1.CR2&size=l").data)).size
    assert max(small) == 480 and max(large) == 1600
    develop_now(env)
    r = c.get("/api/thumb?dev=a/IMG_1.CR2")
    assert r.status_code == 200 and r.data[:2] == b"\xff\xd8"
    assert c.get("/api/thumb?dev=../../x").status_code == 404 and c.get("/api/thumb?dev=nope").status_code == 404
    # a developed JPEG bigger than the small size is shrunk too
    out = env["nas"] / "PhotoManager" / "Processed" / "a" / "IMG_1_developed.jpg"
    out.write_bytes(jpeg(3000, 2000))
    assert max(Image.open(io.BytesIO(c.get("/api/thumb?dev=a/IMG_1.CR2&size=l").data)).size) == 1600
