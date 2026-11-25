# Backend (`app.py`)

One module, sections: Drop access (`:68-118`), stats (`:121-190`), comments (`:193-234`),
admin check (`:237-261`), API (`:264-432`). Social state details are in
[social-state.md](social-state.md); this file covers Drop access and the endpoints.

## Drop access

### `_drop` / `drop_call` / `drop` (`app.py:69-98`)

- `_drop(method, path, body=None, cookie=None)` does a blocking `urllib.request` call to
  `DROP_URL + path`, JSON-encoding `body` if given, always sending
  `Content-Type: application/json` and **`X-Drop: 1`** (Drop's CSRF header, required for its
  changes), plus `Cookie: am_admin=<cookie>` when `cookie` is given, timeout 5 s, and returns
  the parsed JSON. An HTTP **403** from Drop raises `DropRefused(<Drop's detail>)`; other HTTP
  errors are re-raised.
- `drop_call = _drop` is a module-level indirection **so tests can replace it**
  (`qs.drop_call = fake_drop`). Always call through `drop_call`, never `_drop` directly. It's
  called as `drop_call(method, path, body, cookie=cookie)`, so a replacement must accept
  `cookie` as a keyword.
- `async drop(method, path, body=None, cookie=None)` runs `drop_call` in a worker thread.
  `DropRefused` becomes **HTTP 403 with Drop's message** (e.g. "Only the admin can add or
  change things in Drop. ..."). Any other exception (connection refused, timeout, other HTTP
  error, bad JSON) is logged as a warning and turned into
  **HTTP 503 "Can't reach Drop right now. Is it running?"**. The page shows that text in its
  "Can't load the feed" state.

### Drop's `/api/items` format (what QuickShot relies on)

Drop returns `{"version", "items": [...], "admin", ...}` where `items` is a **flat** list,
newest first, each item roughly:
`{id (16 hex), kind: "folder"|"file", name, size, type (MIME), created (epoch float),
pinned, nodownload, parent (folder id or null), kept, download (bool), blocked_by}`.
QuickShot uses `id`, `kind`, `name`, `parent`, `type`, `size`, `created`, `download`.
`download` is Drop's computed "downloads allowed" (false if the item or any ancestor has
downloads off). QuickShot filters out non-dict entries defensively (`app.py:273`).
Reading is open to everyone, so `GET` calls send no cookie.

`POST /api/folders {name, parent}` creates a folder and returns the new item (`id`, `name`,
...). QuickShot only ever calls it with `{"name": FOLDER, "parent": None}`. On the box Drop is
**read-only for everyone but the admin** unless the admin opened it up (Drop's
`PUT api/settings`; platform decision 0026), so this needs the admin's session: QuickShot
forwards the caller's `am_admin` cookie. Standalone Drop (no admin service) lets anyone.

Note that Drop **expires files** after `DROP_KEEP_DAYS` (default 7) unless they or a parent
folder are "kept" (pinned). That's why the empty-state text tells people to tap **keep** on
the folder. An expired clip disappears from the feed and its counts/comments are forgotten.

### `find_folder(items)` (`app.py:101-105`)

Returns the first item that is a folder, **top-level** (`not parent`), whose name, stripped
and `casefold()`ed, equals `FOLDER.casefold()`, and whose id matches `ID_RE`; else `None`.
So `"quickshot "`, `"QUICKSHOT"` match; a folder named QuickShot *inside* another folder does
not (test `OTHER` folder). If several top-level folders match, the first in Drop's order
(newest first) wins.

### `clips_in(items, folder_id)` (`app.py:108-118`)

Breadth-ish walk of descendants: starts with `{folder_id}`, repeatedly scans all items for
folders whose `parent` is in the frontier (id must match `ID_RE`, and not already seen, which
also guards against cycles). Then returns every `kind == "file"` item whose `parent` is in
that set, whose id matches `ID_RE`, and whose `type` matches `VIDEO_RE`. PDFs, images, text
notes in the folder are ignored. O(folders x items), fine for a hotspot's Drop.

### Creators (`app.py:287-294`, inside `feed`)

`folders` maps id -> folder item. `creator(c)` starts at the clip's parent folder and walks
up until it reaches a folder whose parent is the QuickShot folder (or runs out / loops, via
`seen`). Result:
- clip directly in QuickShot -> walk climbs past QuickShot to `None` -> `creator: None`
  (the page shows `@quickshot`);
- `QuickShot/Beach/x.mp4` and `QuickShot/Beach/Sunsets/y.webm` -> `"Beach"`;
- the name is cut to 40 characters. The page lower-cases it and strips characters other than
  letters, digits, `_` and `.` for the `@handle`.

## Endpoints

All paths are relative to the app root (the platform mounts it at `/quickshot/`).
Errors from `HTTPException` are rendered as `{"detail": "..."}` by the custom handler
(`app.py:425-427`). Pydantic body validation errors use FastAPI's default 422 shape.

### `GET /` (`app.py:430-432`)
Serves `static/index.html` with `Cache-Control: no-cache` (so a new image's page is picked up).

### `GET /api/feed?viewer=` (`app.py:270-301`)
No header needed (read-only). Steps:
1. `drop("GET", "/api/items")` (503 if Drop is down).
2. `find_folder`, then `clips_in` (empty list if no folder).
3. **Only if the folder was found**: under each lock, `forget_except(ids)` on stats and on
   comments; save each file only if something was removed. (A missing folder, e.g. renamed,
   or Drop half-loaded, would otherwise look like "every clip was deleted" and wipe all state.)
4. `me = viewer` if it matches `VIEWER_RE`, else `None` (so `liked` is false for bad ids).
5. Response:

```json
{
  "drop_path": "../drop/",
  "folder": {"id": "<16 hex>", "name": "QuickShot"} | null,
  "folder_name": "QuickShot",              // the configured QUICKSHOT_FOLDER, for empty-state text
  "clips": [                                // sorted by created, newest first
    {"id", "name" (<=200 chars), "type", "size" (int), "created" (float),
     "download" (bool, default true), "creator" (str<=40 | null),
     "likes" (int), "views" (int), "liked" (bool, for this viewer), "comments" (int)}
  ]
}
```

Counts are read without taking the locks (single-threaded event loop; reads between awaits
are consistent enough).

### `POST /api/setup` (`app.py:304-314`)
Needs `X-QuickShot: 1` (else 403). Re-reads Drop's items; if no matching folder, creates one
via `drop("POST", "/api/folders", {"name": FOLDER, "parent": None}, cookie=<the caller's
am_admin cookie or None>)` and logs it. Idempotent: returns `{"folder": {"id", "name"}}` for
the existing or new folder. QuickShot doesn't check the admin itself; **Drop decides**: on
the box only the admin can create the folder, unless the admin let everyone change Drop.
Anyone else gets Drop's **403** passed through (`"Only the admin can add or change things in
Drop. ..."`), and the page toasts "Only the admin can create it. Sign in to the admin panel
first." An existing folder is returned to anyone (no write needed). Putting clips into the
folder happens in Drop, under the same rule.

### `POST /api/likes/{cid}` body `{viewer, liked}` (`app.py:331-343`)
Header required. 400 if `cid` isn't 16 hex or `viewer` fails `VIEWER_RE` (`check_clip`).
Under the stats lock: add (if `liked` and fewer than `MAX_LIKERS`) or discard the viewer,
save, return `{"likes": n, "liked": bool}`. Does **not** check that the clip exists in Drop.

### `POST /api/views/{cid}` body `{viewer}` (`app.py:346-360`)
Header required, same validation. Counts a view if this `(cid, viewer)` wasn't counted in the
last `VIEW_GAP`; saves only when counted. Returns `{"views": n}`.

### `GET /api/comments/{cid}?viewer=` (`app.py:374-379`)
No header (read-only). 404 if `cid` isn't 16 hex. Returns
`{"comments": [ {id, name, text, at, mine, can_delete} ...oldest first], "admin": bool}`.
`viewer` isn't format-checked here; an unknown/empty viewer just gets `mine: false`.

### `POST /api/comments/{cid}` body `{viewer, name?, text}` (`app.py:382-403`)
Header required; validation, cleaning, rate limit; returns
`{"comment": {...public form...}, "count": n}`. Errors: 400 empty text, 413 too long,
429 too fast. Details in social-state.md.

### `DELETE /api/comments/{cid}/{comment_id}?viewer=` (`app.py:406-422`)
Header required. 404 for malformed ids or an unknown comment; 403 unless admin or the
comment's own viewer. Returns `{"ok": true, "count": n}`.

## Error handling summary

| Situation | Status | detail |
|---|---|---|
| Drop unreachable / errors | 503 | Can't reach Drop right now. Is it running? |
| Setup by a non-admin while Drop is read-only (Drop's 403) | 403 | Drop's message: Only the admin can add or change things in Drop. ... |
| Missing `X-QuickShot: 1` on a change | 403 | Missing request header. |
| Bad clip id or viewer on like/view/comment post | 400 | Bad request. |
| Bad clip id on comment list | 404 | No such clip. |
| Empty comment | 400 | Write something first. |
| Comment raw length > 350 | 413 | Comments are up to 300 characters. |
| Comment within 3 s of the last | 429 | Slow down a little. |
| Delete someone else's comment | 403 | You can only delete your own comments. |
| Unknown / malformed comment | 404 | No such comment. |
| Missing body field / wrong type | 422 | FastAPI default |
