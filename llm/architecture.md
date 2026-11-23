# Architecture

## The pieces

```
 phone browser                        AirplaneMode box (Docker)
 ─────────────                        ─────────────────────────
 /quickshot/  (index.html) ──Caddy──> quickshot:8000  (FastAPI, app.py, 64m)
   │  GET  api/feed ───────────────>    │  GET http://drop:8000/api/items  (X-Drop: 1)
   │  POST api/likes|views|comments >    │  /data/stats.json, /data/comments.json
   │                                     │  GET http://admin:8000/api/admin/session (am_admin cookie)
   │
   └─ <video src="../drop/api/files/{id}"> ──Caddy──> drop:8000  (Range requests, streams bytes)
```

- **QuickShot backend** (`app.py`): a thin metadata service. It never opens a video file.
  For the feed it reads Drop's full flat item list, picks out the QuickShot folder and the
  videos below it, and decorates each clip with its like/view/comment counts.
- **Drop** (`../airplanemode-drop`): the file store. Uploading clips to QuickShot is just
  uploading into Drop's QuickShot folder. Drop serves `GET api/files/{id}` with HTTP Range
  support, so `<video>` can seek. Drop allows inline streaming of `video/*` even when
  downloads are turned off for an item (its `INLINE_TYPES`); "downloads off" only blocks the
  `?dl=` attachment path for non-admins.
- **Platform admin service** (`../airplanemode`): QuickShot only asks it "is this `am_admin`
  token a live admin session?" so the admin can delete any comment.
- **Caddy** (platform `Caddyfile`): `redir /quickshot /quickshot/` and
  `handle_path /quickshot/* { reverse_proxy quickshot:8000 }`. `handle_path` strips the
  prefix, so the app sees `/`, `/api/feed`, etc. That's why the page must use relative URLs
  (`api/feed`, not `/api/feed`).

## Why the browser streams from Drop

Proxying video through a 64 MB Python container would cost memory, CPU and latency on the
NanoPi, and duplicate Range handling. The feed returns `drop_path` (env `DROP_PATH`, default
`../drop/`), and the page builds `drop_path + "api/files/" + id` (`static/index.html:208`).
On the box `../drop/` is resolved against `/quickshot/` -> `/drop/api/files/{id}`, i.e. the
same origin, through Caddy. In standalone dev `DROP_PATH` is an absolute
`http://localhost:8766/` (`compose.yaml`).

## Request flow: opening the page

1. `GET /quickshot/` -> `index()` (`app.py:383`) serves `static/index.html` with
   `Cache-Control: no-cache`.
2. The page creates/reads its viewer id and name in `localStorage`, then `start()` calls
   `GET api/feed?viewer=<id>` (`static/index.html:409-424`).
3. The backend calls Drop `GET /api/items` (5 s timeout, in a worker thread), finds the folder,
   collects clips, prunes counts for deleted clips (only if the folder was found), and returns
   clips newest first with counts and `liked` for this viewer (`app.py:225-256`).
4. The page builds an ordered list (unseen newest first, then shuffled), makes one `<section>`
   per entry, and attaches `<video>` elements only around the visible slide.
5. Every 30 s it re-fetches the feed to refresh counts and slot in new uploads.

## How it plugs into the platform

- Platform `compose.yaml` service `quickshot`: image
  `ghcr.io/jessewolcott/airplanemode-quickshot:0.1.0` (pinned), env `DROP_URL=http://drop:8000`,
  `ADMIN_URL=http://admin:8000`, volume `quickshot-data:/data`, `mem_limit ${QUICKSHOT_MEM:-64m}`,
  `restart: unless-stopped`. `DROP_PATH` is left at its default `../drop/`.
- Platform `compose.dev.yaml` builds this repo as `airplanemode-quickshot:dev`.
- Platform `portal/apps.json` has the menu entry (`/quickshot/`). Platform CI checks that the
  image tag, Caddy route and apps.json entry all exist.
- `app.yaml` (this repo) is the manifest: id, port 8000, path `/quickshot/`, memory 64m,
  volume `/data`, `needs: {llm: none, apps: [drop]}`, documented env vars.

## Configuration (environment)

| Var | Default | Used at | Meaning |
|---|---|---|---|
| `DATA_DIR` | `/data` | `app.py:44` | Where `stats.json` / `comments.json` live (tests point it at a temp dir) |
| `DROP_URL` | `http://drop:8000` | `app.py:47` | Drop's API from inside the stack (trailing `/` stripped) |
| `DROP_PATH` | `../drop/` | `app.py:48` | Prefix the *browser* streams from; returned verbatim in the feed |
| `QUICKSHOT_FOLDER` | `QuickShot` | `app.py:49` | Name of the top-level Drop folder (matched case-insensitively) |
| `ADMIN_URL` | empty | `app.py:61` | Platform admin service; empty disables admin recognition entirely |

## Constants (`app.py:50-62`)

| Name | Value | Meaning |
|---|---|---|
| `ID_RE` | `^[0-9a-f]{16}$` | Drop item id shape (Drop uses `secrets.token_hex(8)`) |
| `VIEWER_RE` | `^[A-Za-z0-9_-]{8,40}$` | Acceptable viewer id |
| `VIDEO_RE` | `^video/[A-Za-z0-9.+-]+$` | What counts as a clip (by Drop's stored MIME type) |
| `VIEW_GAP` | 1800 s | One counted view per viewer per clip per 30 min |
| `MAX_LIKERS` | 2000 | Cap on likers stored per clip |
| `MAX_COMMENT` | 300 | Comment length (characters, after cleaning) |
| `MAX_NAME` | 24 | Display name length |
| `KEEP_COMMENTS` | 500 | Newest comments kept per clip |
| `COMMENT_GAP` | 3 s | Minimum gap between one viewer's comments |
| `CID_RE` | `^[0-9a-f]{12}$` | Comment id shape (`secrets.token_hex(6)`) |
| `TOKEN_RE` | `^[A-Za-z0-9_-]{16,128}$` | Acceptable `am_admin` cookie shape before asking the admin service |

## Process model

Single uvicorn worker (`CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]`).
All state (stats, comments, admin cache, rate-limit maps) is in-process; the `asyncio.Lock`s
only serialise within that one process. Running more than one worker would break the locks
and let workers overwrite each other's JSON files. Blocking work (HTTP to Drop/admin, file
writes) runs in `anyio.to_thread.run_sync` so the event loop stays responsive.
FastAPI's docs/OpenAPI endpoints are disabled (`app.py:64`).
