"""QuickShot API tests.  Run: python tests/test_quickshot.py"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="quickshot-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as qs  # noqa: E402

H = {"X-QuickShot": "1"}
QS, BEACH, DEEP, OTHER = "a" * 16, "b" * 16, "c" * 16, "d" * 16
C1, C2, C3, C4, PDF = "1" * 16, "2" * 16, "3" * 16, "4" * 16, "5" * 16
ME, YOU = "viewer-me-123456", "viewer-you-654321"
ITEMS = [
    {"id": QS, "kind": "folder", "name": "quickshot ", "parent": None},
    {"id": BEACH, "kind": "folder", "name": "Beach", "parent": QS},
    {"id": DEEP, "kind": "folder", "name": "Sunsets", "parent": BEACH},
    {"id": OTHER, "kind": "folder", "name": "QuickShot", "parent": BEACH},   # not top-level: ignored
    {"id": C1, "kind": "file", "name": "waves #ocean.mp4", "type": "video/mp4", "parent": QS, "created": 10},
    {"id": C2, "kind": "file", "name": "dive.mov", "type": "video/quicktime", "parent": BEACH, "created": 30},
    {"id": C3, "kind": "file", "name": "glow.webm", "type": "video/webm", "parent": DEEP, "created": 20, "download": False},
    {"id": C4, "kind": "file", "name": "elsewhere.mp4", "type": "video/mp4", "parent": None, "created": 40},
    {"id": PDF, "kind": "file", "name": "map.pdf", "type": "application/pdf", "parent": QS, "created": 50},
]
drop_state = {"items": list(ITEMS), "created": []}


def fake_drop(method, path, body=None):
    if method == "GET" and path == "/api/items":
        return {"items": drop_state["items"]}
    if method == "POST" and path == "/api/folders":
        f = {"id": "e" * 16, "kind": "folder", "name": body["name"], "parent": None}
        drop_state["items"].append(f)
        drop_state["created"].append(f)
        return f
    raise AssertionError((method, path))


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def feed(viewer=ME):
    return ok(c.get("/api/feed", params={"viewer": viewer}))


def test_page_uses_relative_urls():
    html = c.get("/").text
    assert '"api/feed' in html and '"/api/' not in html
    assert not re.search(r'(src|href)="(https?:|//|/[^/])', html), re.findall(r'(?:src|href)="[^"]*"', html)


def test_feed_is_the_quickshot_folder():
    d = feed()
    assert d["folder"]["id"] == QS and d["drop_path"] == "../drop/"
    assert [x["id"] for x in d["clips"]] == [C2, C3, C1]            # newest first; no pdf, nothing outside
    by = {x["id"]: x for x in d["clips"]}
    assert by[C1]["creator"] is None and by[C2]["creator"] == "Beach" and by[C3]["creator"] == "Beach"
    assert by[C3]["download"] is False and by[C1]["likes"] == 0 and by[C1]["comments"] == 0


def test_setup_creates_the_folder_once():
    drop_state["items"] = [i for i in ITEMS if i["id"] != QS]
    try:
        assert feed()["folder"] is None
        assert c.post("/api/setup").status_code == 403
        f = ok(c.post("/api/setup", headers=H))["folder"]
        assert f["name"] == "QuickShot" and len(drop_state["created"]) == 1
        ok(c.post("/api/setup", headers=H))                           # already there: nothing new
        assert len(drop_state["created"]) == 1
    finally:
        drop_state["items"] = list(ITEMS)


def test_drop_down():
    qs.drop_call = lambda *a: (_ for _ in ()).throw(OSError("refused"))
    try:
        r = c.get("/api/feed")
        assert r.status_code == 503 and "Drop" in r.json()["detail"]
    finally:
        qs.drop_call = fake_drop


def test_likes_once_per_viewer():
    assert c.post(f"/api/likes/{C1}", json={"viewer": ME, "liked": True}).status_code == 403
    assert ok(c.post(f"/api/likes/{C1}", json={"viewer": ME, "liked": True}, headers=H))["likes"] == 1
    assert ok(c.post(f"/api/likes/{C1}", json={"viewer": ME, "liked": True}, headers=H))["likes"] == 1
    assert ok(c.post(f"/api/likes/{C1}", json={"viewer": YOU, "liked": True}, headers=H))["likes"] == 2
    by = {x["id"]: x for x in feed(ME)["clips"]}
    assert by[C1]["likes"] == 2 and by[C1]["liked"] is True
    assert {x["id"]: x for x in feed("viewer-third-0000")["clips"]}[C1]["liked"] is False
    assert ok(c.post(f"/api/likes/{C1}", json={"viewer": ME, "liked": False}, headers=H)) == {"likes": 1, "liked": False}
    assert c.post(f"/api/likes/{C1}", json={"viewer": "bad id!", "liked": True}, headers=H).status_code == 400
    assert c.post("/api/likes/nope", json={"viewer": ME, "liked": True}, headers=H).status_code == 400


def test_views_once_per_viewer_per_half_hour():
    assert ok(c.post(f"/api/views/{C2}", json={"viewer": ME}, headers=H))["views"] == 1
    assert ok(c.post(f"/api/views/{C2}", json={"viewer": ME}, headers=H))["views"] == 1
    assert ok(c.post(f"/api/views/{C2}", json={"viewer": YOU}, headers=H))["views"] == 2
    st = qs.get_stats()
    st.recent[(C2, ME)] -= qs.VIEW_GAP + 1
    assert ok(c.post(f"/api/views/{C2}", json={"viewer": ME}, headers=H))["views"] == 3


def post(cid, viewer, text, name="Sam"):
    qs.get_comments().last.pop(viewer, None)       # skip the cooldown between test posts
    return c.post(f"/api/comments/{cid}", json={"viewer": viewer, "name": name, "text": text}, headers=H)


def test_comments():
    mine = ok(post(C1, ME, "  so good \x07\n\n\n\nwow  "))["comment"]
    assert mine["text"] == "so good \n\nwow" and mine["mine"] and mine["name"] == "Sam"
    theirs = ok(post(C1, YOU, "first!", name="  Alex\nB  "))["comment"]
    assert theirs["name"] == "Alex B"
    lst = ok(c.get(f"/api/comments/{C1}", params={"viewer": ME}))["comments"]
    assert [x["text"] for x in lst] == ["so good \n\nwow", "first!"]
    assert [x["mine"] for x in lst] == [True, False] and [x["can_delete"] for x in lst] == [True, False]
    assert "viewer" not in json.dumps(lst) and ME not in json.dumps(lst)         # ids never leak
    assert {x["id"]: x for x in feed()["clips"]}[C1]["comments"] == 2
    # rules
    assert c.post(f"/api/comments/{C1}", json={"viewer": ME, "text": "hi"}).status_code == 403
    assert post(C1, ME, "   ").status_code == 400
    assert post(C1, ME, "x" * 400).status_code == 413
    ok(post(C2, ME, "one"))
    r = c.post(f"/api/comments/{C2}", json={"viewer": ME, "text": "two"}, headers=H)   # too fast
    assert r.status_code == 429
    # delete: only your own
    assert c.delete(f"/api/comments/{C1}/{theirs['id']}", params={"viewer": ME}, headers=H).status_code == 403
    assert c.delete(f"/api/comments/{C1}/{mine['id']}", params={"viewer": ME}).status_code == 403   # no header
    assert ok(c.delete(f"/api/comments/{C1}/{mine['id']}", params={"viewer": ME}, headers=H))["count"] == 1
    assert c.delete(f"/api/comments/{C1}/zzzz", params={"viewer": ME}, headers=H).status_code == 404


def test_admin_can_delete_any_comment():
    theirs = ok(post(C3, YOU, "spam spam"))["comment"]
    qs.ADMIN_URL, qs.check_admin_token = "http://admin.test", lambda t: t == "a" * 32
    qs._admin_cache.clear()
    try:
        c.cookies.set("am_admin", "b" * 32)
        assert c.delete(f"/api/comments/{C3}/{theirs['id']}", params={"viewer": ME}, headers=H).status_code == 403
        c.cookies.set("am_admin", "a" * 32)
        lst = ok(c.get(f"/api/comments/{C3}", params={"viewer": ME}))
        assert lst["admin"] and lst["comments"][0]["can_delete"] and not lst["comments"][0]["mine"]
        ok(c.delete(f"/api/comments/{C3}/{theirs['id']}", params={"viewer": ME}, headers=H))
    finally:
        c.cookies.clear()
        qs.ADMIN_URL, qs.check_admin_token = "", qs._ask_admin_service
        qs._admin_cache.clear()


def test_everything_survives_a_restart():
    ok(post(C2, YOU, "still here"))
    qs.stats, qs.comments = None, None                 # as if the container restarted
    d = {x["id"]: x for x in feed(YOU)["clips"]}
    assert d[C1]["likes"] == 1 and d[C2]["views"] == 3
    assert "still here" in [x["text"] for x in ok(c.get(f"/api/comments/{C2}"))["comments"]]
    assert (qs.DATA / "comments.json").exists() and (qs.DATA / "stats.json").exists()


def test_deleted_clips_lose_their_counts():
    drop_state["items"] = [i for i in ITEMS if i["id"] != C2]
    try:
        feed()
        assert C2 not in qs.get_stats().views and C2 not in qs.get_comments().by_clip
        # but if the folder itself is missing (renamed, or Drop half-loaded), keep everything
        drop_state["items"] = [i for i in ITEMS if i["id"] != QS]
        feed()
        assert C1 in qs.get_stats().likes
    finally:
        drop_state["items"] = list(ITEMS)


if __name__ == "__main__":
    qs.drop_call = fake_drop
    with TestClient(qs.app) as c:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok  ", name)
