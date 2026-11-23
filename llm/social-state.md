# Social state: likes, views, comments

All of it is owned by QuickShot and lives in its volume (`DATA_DIR`, default `/data`; on the
box the named volume `quickshot-data`), so it survives restarts and image updates.

## Persistence model

Two singletons, created **lazily** on first use:

| Object | Getter | File | In memory only |
|---|---|---|---|
| `Stats` (`app.py:108-139`) | `get_stats()` (`:145`) | `stats.json` | `recent` (view dedupe) |
| `Comments` (`app.py:152-178`) | `get_comments()` (`:184`) | `comments.json` | `last` (rate limit) |

- Lazy creation means the `asyncio.Lock` in each `__init__` is created inside the running
  event loop, and tests can simulate a restart with `qs.stats, qs.comments = None, None`.
- Loading: `FileNotFoundError` -> start empty silently; any other error (corrupt JSON, wrong
  shape) -> rename the file to `<name>.bad-<unix time>`, log a warning and start empty. The
  damaged file is kept for a look, not overwritten by the next save (test
  `test_a_damaged_file_is_kept_not_overwritten`).
- Saving (`save()`): `DATA.mkdir(parents=True, exist_ok=True)`, write a sibling temp file
  (`stats.tmp` / `comments.tmp`, via `with_suffix(".tmp")`), then `tmp.replace(target)`.
  `replace` is an atomic rename on the same filesystem, so a crash leaves either the old or
  the new file, never half of one.
- Every save is called **while holding the object's lock** and runs in a worker thread
  (`await anyio.to_thread.run_sync(st.save)`), so saves are serialised and never interleave
  with mutations. Rule: mutate and save inside `async with x.lock:`.
- Each mutation rewrites the whole file. Fine at hotspot scale (dozens of people).

### `stats.json`
```json
{"likes": {"<clip id>": ["<viewer id>", ...sorted]}, "views": {"<clip id>": 12}}
```
In memory `likes` is `clip -> set(viewer)`, `views` is `clip -> int`. Empty like-sets are
dropped when saving.

### `comments.json`
```json
{"clips": {"<clip id>": [{"id": "<12 hex>", "viewer": "<viewer id>", "name": "Sam",
                         "text": "so good", "at": 1763700000.123}, ...oldest first]}}
```
Written with `ensure_ascii=False` (emoji/accents stay readable). Clips with empty lists are
dropped when saving.

## Likes (`app.py:290-302`)

- Per-viewer set per clip, so a like counts once per viewer (per browser). Liking again is a
  no-op; `liked: false` removes it.
- `MAX_LIKERS = 2000` per clip: further likes are silently ignored (the response's `liked`
  will be false). Unlikes always work.
- Response `{"likes", "liked"}` is the authoritative value the page reconciles to.
- The feed reports `likes = len(set)` and `liked = viewer in set` (`app.py:258-259`).
- Clearing browser storage creates a new viewer; its old like stays counted (decision 0014).

## Views (`app.py:305-319`)

- `recent[(clip, viewer)] = time` of the last counted view, memory only.
- A view counts if `now - recent.get(key, 0) >= VIEW_GAP` (30 min). Then increment, save.
- When `recent` exceeds 20,000 entries it's pruned to entries newer than `VIEW_GAP`.
- Because `recent` isn't persisted, a restart lets each viewer count one more view per clip.
- The page only posts a view after the clip has been on screen for 2 s (see feed-page.md).

## Comments

### Posting (`app.py:341-362`)
Order of checks matters:
1. `X-QuickShot` header (403), then `check_clip` (400 for bad clip id / viewer).
2. `text = clean_text(body.text, 300)`. If empty -> **400 "Write something first."**
3. If the **raw** `body.text` is longer than `MAX_COMMENT + 50` (350) -> **413**. So raw input
   of 301-350 characters is silently truncated to 300 rather than rejected (slack for
   whitespace and control characters that cleaning removes). The page's textarea has
   `maxlength="300"`.
4. `name = clean_text(body.name, 24)` with newlines turned into spaces, or `"someone"` if empty.
5. Under the comments lock: if this viewer's last comment was less than `COMMENT_GAP` (3 s)
   ago -> **429 "Slow down a little."** Otherwise record `last[viewer] = now`.
6. New comment `{"id": secrets.token_hex(6), viewer, name, text, at: now}` appended to the
   clip's list; `del lst[:-KEEP_COMMENTS]` keeps the newest 500; save.
7. Returns `{"comment": public(c, viewer), "count": len(lst)}`.

The rate limit is per viewer id (which the client chooses), so it's a politeness limit, not
an abuse control. `last` grows by one entry per distinct commenting viewer and is never pruned
(memory only; tiny at hotspot scale).

### `clean_text(text, limit)` (`app.py:191-195`)
1. Unicode NFC normalise (`None` -> `""`).
2. Drop every character whose Unicode category starts with `C` (control, format, unassigned,
   private use, surrogates), **except `\n`**. That removes `\x07`, `\r`, tabs, zero-width
   joiners and bidi overrides, among others.
3. Collapse 3+ consecutive newlines to 2; strip leading/trailing whitespace.
4. Truncate to `limit` characters (after stripping).

Test example: `"  so good \x07\n\n\n\nwow  "` -> `"so good \n\nwow"`; name `"  Alex\nB  "` ->
`"Alex B"`.

### Name rules
- Free text, not unique, not verified; anyone can use any name (moderation is the admin's
  delete). Up to 24 characters after cleaning; newlines become spaces; empty -> `"someone"`.
- The page defaults to `traveler-NNNN` (random 1000-9999) and stores it in `localStorage`.
  Changing the name only affects future comments; stored comments keep the name they were
  posted with.

### Listing (`app.py:328-338`)
`public(c, viewer, admin)` is the only shape a comment ever leaves the server in:
`{id, name, text, at, mine: c.viewer == viewer, can_delete: admin or mine}`. The stored
`viewer` field is never included. The list response also carries `admin` (whether the caller
is the admin). Oldest first.

### Deleting (`app.py:365-381`)
- Malformed clip or comment id -> 404. Admin check happens (possibly a network call to the
  admin service) **before** taking the lock.
- Under the lock: find the comment (404 if missing); allowed if admin, or if `viewer` matches
  `VIEWER_RE` **and** equals the comment's stored viewer; else 403.
- Remove, save, return `{"ok": true, "count": n}`.

## Forgetting deleted clips (`app.py:239-245`, `:133-139`, `:174-178`)

On every `GET /api/feed`, if the QuickShot folder was found, each store's
`forget_except(ids)` drops entries for clip ids not currently in the folder tree (likes,
views and comments), and saves only if something changed. Consequences:
- Deleting a clip in Drop, Drop's expiry sweep removing it, **or moving it out of the
  QuickShot folder tree** all erase its counts and comments on the next feed load.
- If the folder is *not* found (renamed, deleted, Drop returning a partial list) nothing is
  forgotten. A test covers this (`test_deleted_clips_lose_their_counts`).
- `recent` (view dedupe) and `last` (rate limit) are not pruned by this.
- Likes/views can be posted for any well-formed id, even one not in Drop; such entries
  linger until the next feed load prunes them.

## Locks

| Lock | Protects | Taken by |
|---|---|---|
| `Stats.lock` | `likes`, `views`, `recent`, `stats.json` | like, view, feed (forget) |
| `Comments.lock` | `by_clip`, `last`, `comments.json` | add, delete, feed (forget) |

The feed takes them one after the other, never nested, so there's no lock-ordering risk.
Reads for the feed and comment lists don't take locks.
