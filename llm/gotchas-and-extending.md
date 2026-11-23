# Gotchas and how to extend

## Gotchas and quirks (as the code stands)

Backend:
- **Missing folder must not wipe state.** `feed` only calls `forget_except` when
  `find_folder` succeeded (`app.py:233`). Keep that guard on any new per-clip state.
- **Moving a clip out of the QuickShot tree counts as deleting it**: its likes, views and
  comments are erased on the next feed load. So does Drop's expiry (default 7 days unless the
  folder is "kept"). Moving it back later starts from zero.
- **Corrupt state file -> silent reset.** A JSON parse error logs a warning and starts empty;
  the next save overwrites the bad file (`app.py:120-121`, `:158-159`).
- **View dedupe is in memory**: a restart allows one extra counted view per viewer per clip.
- **Likes/views accept any well-formed clip id**, existing or not; the next feed load prunes
  unknown ones.
- **Comment length**: 301-350 raw characters are truncated to 300, not rejected; only > 350
  gives 413. The emptiness check runs before the length check.
- **Admin answers are cached 30 s**, including after sign-out; the cache resets at 257 tokens.
- **Single process only.** Locks are `asyncio.Lock`s and state is in memory; multiple
  uvicorn workers would corrupt the JSON files. Don't add `--workers`.
- **Drop calls are synchronous urllib in threads with a 5 s timeout.** Every feed request
  (and every 30 s refresh from every phone) fetches Drop's whole item list.
- `Comments.last` (rate-limit map) is never pruned; fine for a hotspot.
- `drop_call` / `check_admin_token` are indirections for tests; call through them.

Page:
- **The relative-URL test is strict**: no `src="/..."`, `href="/..."`, `http(s):` or `//` in
  `src`/`href` attributes, and no `"/api/` string anywhere in the HTML. Build URLs relative
  (`api/...`, `../`).
- **Slides accumulate**: each round appends a `<section>` per clip and none are removed (only
  their videos). A very long session grows the DOM slowly; videos stay capped at 5.
- **Re-entering a slide restarts it** (`currentTime = 0`).
- **Sound fallback**: if an unmuted `play()` is refused, the clip plays muted but `unmuted`
  stays true, so the rail shows "On".
- **Double-tap only likes**, never unlikes. Single taps are delayed 280 ms (to detect a
  double-tap) before pausing.
- **Gone clips** are hidden (`display: none`) except the one on screen; they stay in
  `slides`, and still count toward the +/-2 window by index.
- The 30 s refresh ignores errors and empty lists, so if the folder is emptied the feed keeps
  playing what it has (videos then fail to load from Drop) until reload.
- `index()`'s comment ("the server sends the folder path") is stale; creators come from the
  server's `creator` field.
- With one clip, `nextRound` treats the whole order as recent (`slice(-0)`); harmless.
- `localStorage` failures are swallowed: without storage you get a new viewer id and default
  name on every load, and `seen` isn't kept.
- Autoplay requires `muted` + `playsinline` (iOS); keep both on new video elements.

Platform:
- Caddy's `handle_path` strips `/quickshot`, so the app's routes are rooted at `/`.
- `DROP_PATH` default `../drop/` assumes Drop is mounted at `/drop/` on the same origin.

## How to extend

### Add an endpoint
- Put it in `app.py` under the API section. If it changes state, call `need_header(request)`
  first and send `X-QuickShot: 1` from the page (use the `H` constant).
- Validate ids with `ID_RE` / `VIEWER_RE` / `CID_RE` (`check_clip` for clip + viewer).
- Raise `HTTPException(status, "Human sentence.")`; the page shows `detail` to users.
- Use a relative URL in the page. Add a test to `tests/test_quickshot.py` (remember tests run
  in order and share state).

### Add new persistent per-clip state (e.g. reactions, bookmarks)
- Either extend `Stats` (and its `save`/load JSON shape, keeping old files loadable with
  `.get(..., {})`) or add a sibling class with its own file, lock, lazy getter and atomic
  `save()` (temp file + `replace`, inside the lock, via `anyio.to_thread.run_sync`).
- Add it to the forget step in `feed`, **inside** the `if folder:` guard.
- Never return viewer ids; compute per-caller booleans like `liked` / `mine`.
- Add a restart test (reset the singleton to `None`, re-read).

### Change what counts as a clip or a creator
- Clip filter: `VIDEO_RE` and `clips_in` (`app.py:94-104`). Note browsers can't play every
  `video/*` type (e.g. some `.mov`/HEVC); the page has no per-clip error fallback today, a
  failed clip just shows the spinner.
- Creator: `creator()` inside `feed` (`app.py:244-249`) and the page's `creator()` handle
  formatting (`static/index.html:155`).

### Change ordering or playback
- Ordering: `nextRound` / `addRound` (`static/index.html:157-231`).
- Window size: the `<= 2` in `activate` (`:243`). Keep it small; CLAUDE.md fixes it at +/-2
  and each extra `<video>` with `preload=auto` costs bandwidth on Drop and phone memory.
- View threshold: the `2000` ms timer in `activate` (`:253-257`); server dedupe `VIEW_GAP`.

### Admin features
- Use `await is_admin(request)` (cached, fails closed). Only meaningful when `ADMIN_URL` is
  set (on the box: `http://admin:8000`). Fake it in tests by setting `qs.ADMIN_URL` and
  `qs.check_admin_token`, and clear `qs._admin_cache`.

### New env var
- Read it at the top of `app.py`, document it in `app.yaml` `env:`, the README settings
  table, and (if the box needs a non-default) the platform `compose.yaml`.

### Before shipping
- `python tests/test_quickshot.py` passes; check the page in the platform dev stack at
  `/quickshot/`; retake `docs/screenshots/` if the UI changed; mark anything not verified on
  hardware as *untested*; ask before pushing a `v*` tag.
