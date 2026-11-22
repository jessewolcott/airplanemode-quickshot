"""QuickShot: an endless vertical feed of the short videos in Drop's "QuickShot" folder.

QuickShot has no videos of its own. Put clips in a top-level Drop folder named QuickShot (any
subfolders count too) and they show up in the feed on every device on the hotspot. The browser
streams them straight from Drop, so QuickShot never touches the bytes.

To feel like a real feed, likes, views and comments are shared by everyone on the hotspot.
Each browser makes up a random viewer id (no accounts) and keeps it to itself: the API never
hands one viewer's id to another. A like counts once per viewer, a view once per viewer per
video every 30 minutes. Comments are kept in the app's volume (/data/comments.json), so they
survive restarts and updates. People can delete their own; the AirplaneMode admin (whoever is
signed in to the admin panel, checked like Drop does) can delete any.

API (relative to the app's root; the platform serves it at /quickshot/):
  GET  api/feed                  the clips (newest first), their like/view counts, where to stream from
  POST api/setup                 create the QuickShot folder in Drop if it's missing
  POST api/likes/{id} {viewer, liked}
  POST api/views/{id} {viewer}
  GET  api/comments/{id}?viewer=          oldest first, each with "mine"
  POST api/comments/{id} {viewer, name, text}
  DELETE api/comments/{id}/{comment}?viewer=
Changes need the header "X-QuickShot: 1" (blocks cross-site requests).
"""
import asyncio
import json
import logging
import os
import re
import secrets
import unicodedata
import time
import urllib.request
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

log = logging.getLogger("quickshot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", "/data"))
STATS = DATA / "stats.json"
COMMENTS = DATA / "comments.json"
DROP_URL = os.getenv("DROP_URL", "http://drop:8000").rstrip("/")
DROP_PATH = os.getenv("DROP_PATH", "../drop/")
FOLDER = os.getenv("QUICKSHOT_FOLDER", "QuickShot")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
VIEWER_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
VIDEO_RE = re.compile(r"^video/[A-Za-z0-9.+-]+$")
VIEW_GAP = 30 * 60
MAX_LIKERS = 2000            # per clip; way more than devices on a hotspot
MAX_COMMENT = 300            # characters
MAX_NAME = 24
KEEP_COMMENTS = 500          # per clip: the newest
COMMENT_GAP = 3              # seconds between one viewer's comments
CID_RE = re.compile(r"^[0-9a-f]{12}$")
# The platform's admin service, to recognise the admin (who can delete any comment).
ADMIN_URL = os.getenv("ADMIN_URL", "").rstrip("/")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- Drop ----------------
def _drop(method, path, body=None):
    req = urllib.request.Request(f"{DROP_URL}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Drop": "1"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


drop_call = _drop        # tests replace this


async def drop(method, path, body=None):
    try:
        return await anyio.to_thread.run_sync(drop_call, method, path, body)
    except Exception as e:
        log.warning("Drop call %s %s failed: %s", method, path, e)
        raise HTTPException(503, "Can't reach Drop right now. Is it running?")


def find_folder(items):
    """The top-level folder named QuickShot (any capitalisation), or None."""
    want = FOLDER.casefold()
    return next((i for i in items if i.get("kind") == "folder" and not i.get("parent")
                 and str(i.get("name", "")).strip().casefold() == want and ID_RE.match(str(i.get("id", "")))), None)


def clips_in(items, folder_id):
    """Videos in the folder or any folder below it."""
    below, todo = {folder_id}, [folder_id]
    while todo:
        fid = todo.pop()
        for i in items:
            if i.get("kind") == "folder" and i.get("parent") == fid and ID_RE.match(str(i.get("id", ""))) and i["id"] not in below:
                below.add(i["id"])
                todo.append(i["id"])
    return [i for i in items if i.get("kind") == "file" and i.get("parent") in below
            and ID_RE.match(str(i.get("id", ""))) and VIDEO_RE.match(str(i.get("type", "")))]


# ---------------- likes and views (one small JSON file) ----------------
class Stats:
    def __init__(self):
        self.likes = {}          # clip id -> set of viewer ids
        self.views = {}          # clip id -> count
        self.recent = {}         # (clip, viewer) -> last counted view (memory only)
        self.lock = asyncio.Lock()
        try:
            d = json.loads(STATS.read_text(encoding="utf-8"))
            self.likes = {k: set(v) for k, v in d.get("likes", {}).items()}
            self.views = {k: int(v) for k, v in d.get("views", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not read %s: %s", STATS, e)

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATS.with_suffix(".tmp")
        tmp.write_text(json.dumps({"likes": {k: sorted(v) for k, v in self.likes.items() if v},
                                   "views": self.views}), encoding="utf-8")
        tmp.replace(STATS)

    def forget_except(self, ids):
        """Clips deleted from Drop: drop their counts."""
        gone = (set(self.likes) | set(self.views)) - ids
        for k in gone:
            self.likes.pop(k, None)
            self.views.pop(k, None)
        return bool(gone)


stats = None


def get_stats():
    global stats
    if stats is None:
        stats = Stats()
    return stats


class Comments:
    def __init__(self):
        self.by_clip = {}        # clip id -> [{id, viewer, name, text, at}]
        self.last = {}           # viewer -> time of their last comment (memory only)
        self.lock = asyncio.Lock()
        try:
            self.by_clip = json.loads(COMMENTS.read_text(encoding="utf-8")).get("clips", {})
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not read %s: %s", COMMENTS, e)

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = COMMENTS.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clips": {k: v for k, v in self.by_clip.items() if v}}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(COMMENTS)

    def forget_except(self, ids):
        gone = set(self.by_clip) - ids
        for k in gone:
            del self.by_clip[k]
        return bool(gone)


comments = None


def get_comments():
    global comments
    if comments is None:
        comments = Comments()
    return comments


def clean_text(text, limit):
    text = unicodedata.normalize("NFC", text or "")
    text = "".join(ch for ch in text if ch in "\n" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _ask_admin_service(token):
    req = urllib.request.Request(f"{ADMIN_URL}/api/admin/session", headers={"Cookie": f"am_admin={token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


check_admin_token = _ask_admin_service   # tests replace this
_admin_cache = {}


async def is_admin(request: Request):
    token = request.cookies.get("am_admin", "")
    if not ADMIN_URL or not TOKEN_RE.match(token):
        return False
    hit = _admin_cache.get(token)
    if hit and hit[1] > time.time():
        return hit[0]
    answer = await anyio.to_thread.run_sync(check_admin_token, token)
    if len(_admin_cache) > 256:
        _admin_cache.clear()
    _admin_cache[token] = (answer, time.time() + 30)
    return answer


# ---------------- API ----------------
def need_header(request: Request):
    if request.headers.get("x-quickshot") != "1":
        raise HTTPException(403, "Missing request header.")


@app.get("/api/feed")
async def feed(viewer: str = ""):
    data = await drop("GET", "/api/items")
    items = [i for i in data.get("items", []) if isinstance(i, dict)]
    folder = find_folder(items)
    clips = clips_in(items, folder["id"]) if folder else []
    st, cm = get_stats(), get_comments()
    ids = {c["id"] for c in clips}
    if folder:      # no folder (renamed? Drop down?) would look like "everything deleted": keep counts
        async with st.lock:
            if st.forget_except(ids):
                await anyio.to_thread.run_sync(st.save)
        async with cm.lock:
            if cm.forget_except(ids):
                await anyio.to_thread.run_sync(cm.save)
    me = viewer if VIEWER_RE.match(viewer) else None
    # the subfolder right under QuickShot is the clip's "creator" (QuickShot/Beach -> @beach)
    folders = {i["id"]: i for i in items if i.get("kind") == "folder" and ID_RE.match(str(i.get("id", "")))}

    def creator(c):
        f, seen = folders.get(c.get("parent")), set()
        while f and f.get("parent") != folder["id"] and f["id"] not in seen:
            seen.add(f["id"])
            f = folders.get(f.get("parent"))
        return str(f["name"])[:40] if f and f["id"] != folder["id"] else None
    out = [{"id": c["id"], "name": str(c.get("name", ""))[:200], "type": c["type"], "size": int(c.get("size") or 0),
            "created": float(c.get("created") or 0), "download": bool(c.get("download", True)),
            "creator": creator(c), "likes": len(st.likes.get(c["id"], ())), "views": st.views.get(c["id"], 0),
            "liked": bool(me and me in st.likes.get(c["id"], ())), "comments": len(cm.by_clip.get(c["id"], ()))}
           for c in sorted(clips, key=lambda c: -float(c.get("created") or 0))]
    return {"drop_path": DROP_PATH, "folder": {"id": folder["id"], "name": folder["name"]} if folder else None,
            "folder_name": FOLDER, "clips": out}


@app.post("/api/setup")
async def setup(request: Request):
    need_header(request)
    data = await drop("GET", "/api/items")
    folder = find_folder([i for i in data.get("items", []) if isinstance(i, dict)])
    if not folder:
        folder = await drop("POST", "/api/folders", {"name": FOLDER, "parent": None})
        log.info("created the %s folder in Drop", FOLDER)
    return {"folder": {"id": folder["id"], "name": folder["name"]}}


class Like(BaseModel):
    viewer: str
    liked: bool


class View(BaseModel):
    viewer: str


async def check_clip(cid, viewer):
    if not ID_RE.match(cid) or not VIEWER_RE.match(viewer):
        raise HTTPException(400, "Bad request.")


@app.post("/api/likes/{cid}")
async def like(cid: str, body: Like, request: Request):
    need_header(request)
    await check_clip(cid, body.viewer)
    st = get_stats()
    async with st.lock:
        who = st.likes.setdefault(cid, set())
        if body.liked and len(who) < MAX_LIKERS:
            who.add(body.viewer)
        elif not body.liked:
            who.discard(body.viewer)
        await anyio.to_thread.run_sync(st.save)
        return {"likes": len(who), "liked": body.viewer in who}


@app.post("/api/views/{cid}")
async def view(cid: str, body: View, request: Request):
    need_header(request)
    await check_clip(cid, body.viewer)
    st = get_stats()
    now = time.time()
    async with st.lock:
        key = (cid, body.viewer)
        if now - st.recent.get(key, 0) >= VIEW_GAP:
            st.recent[key] = now
            st.views[cid] = st.views.get(cid, 0) + 1
            if len(st.recent) > 20000:
                st.recent = {k: t for k, t in st.recent.items() if now - t < VIEW_GAP}
            await anyio.to_thread.run_sync(st.save)
        return {"views": st.views.get(cid, 0)}


class NewComment(BaseModel):
    viewer: str
    name: str = ""
    text: str


def public(c, viewer, admin=False):
    return {"id": c["id"], "name": c["name"], "text": c["text"], "at": c["at"],
            "mine": c["viewer"] == viewer, "can_delete": admin or c["viewer"] == viewer}


@app.get("/api/comments/{cid}")
async def list_comments(cid: str, request: Request, viewer: str = ""):
    if not ID_RE.match(cid):
        raise HTTPException(404, "No such clip.")
    admin = await is_admin(request)
    return {"comments": [public(c, viewer, admin) for c in get_comments().by_clip.get(cid, [])], "admin": admin}


@app.post("/api/comments/{cid}")
async def add_comment(cid: str, body: NewComment, request: Request):
    need_header(request)
    await check_clip(cid, body.viewer)
    text = clean_text(body.text, MAX_COMMENT)
    if not text:
        raise HTTPException(400, "Write something first.")
    if len(body.text) > MAX_COMMENT + 50:
        raise HTTPException(413, f"Comments are up to {MAX_COMMENT} characters.")
    name = clean_text(body.name, MAX_NAME).replace("\n", " ") or "someone"
    cm = get_comments()
    async with cm.lock:
        now = time.time()
        if now - cm.last.get(body.viewer, 0) < COMMENT_GAP:
            raise HTTPException(429, "Slow down a little.")
        cm.last[body.viewer] = now
        c = {"id": secrets.token_hex(6), "viewer": body.viewer, "name": name, "text": text, "at": now}
        lst = cm.by_clip.setdefault(cid, [])
        lst.append(c)
        del lst[:-KEEP_COMMENTS]
        await anyio.to_thread.run_sync(cm.save)
        return {"comment": public(c, body.viewer), "count": len(lst)}


@app.delete("/api/comments/{cid}/{comment_id}")
async def delete_comment(cid: str, comment_id: str, request: Request, viewer: str = ""):
    need_header(request)
    if not ID_RE.match(cid) or not CID_RE.match(comment_id):
        raise HTTPException(404, "No such comment.")
    cm = get_comments()
    admin = await is_admin(request)
    async with cm.lock:
        lst = cm.by_clip.get(cid, [])
        c = next((c for c in lst if c["id"] == comment_id), None)
        if not c:
            raise HTTPException(404, "No such comment.")
        if not admin and not (VIEWER_RE.match(viewer) and c["viewer"] == viewer):
            raise HTTPException(403, "You can only delete your own comments.")
        lst.remove(c)
        await anyio.to_thread.run_sync(cm.save)
        return {"ok": True, "count": len(lst)}


@app.exception_handler(HTTPException)
async def http_error(_req, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})
