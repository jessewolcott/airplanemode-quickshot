# Security and privacy

## Trust model

The hotspot password is the access control (decision 0014, same as Drop). Anyone on the
hotspot can read the feed, like, view, comment under any name and create the QuickShot folder.
There are no accounts. The only privileged role is the AirplaneMode admin, who can delete any
comment. Moderation = the admin's delete button.

## Viewer ids are private

- Each browser makes a random 20-character id (`static/index.html:153-154`) and keeps it in
  `localStorage` (`qs.viewer`). It's effectively a bearer secret: whoever knows it can unlike
  that viewer's likes and delete that viewer's comments.
- So the API **never returns any viewer id**. Instead responses carry values computed for the
  caller: `liked` in the feed and like response, `mine` / `can_delete` on comments. The only
  serialiser for comments is `public()` (`app.py:328-330`), which omits `viewer`.
- `test_comments` asserts `"viewer"` and the viewer id don't appear in the comment list JSON.
- Viewer ids are stored server-side in `stats.json` (like sets) and `comments.json` (comment
  author). They're random strings, not tied to MAC/IP.
- Viewer ids are validated with `VIEWER_RE` (`^[A-Za-z0-9_-]{8,40}$`) before being stored.
- Viewer ids appear in query strings (`?viewer=`) for GETs and DELETE; Caddy access logs, if
  enabled, could record them. They're also logged nowhere by the app itself.

## Admin recognition (`app.py:198-222`)

Same scheme as Drop (decision 0013 in the platform repo):
1. Read the `am_admin` cookie. The platform admin panel sets it on the same origin (the box),
   so the browser sends it to `/quickshot/` too.
2. If `ADMIN_URL` is empty or the token doesn't match `TOKEN_RE` (`^[A-Za-z0-9_-]{16,128}$`),
   not admin, without any network call.
3. Cache: `_admin_cache[token] = (answer, expires)`; a hit younger than **30 s** is reused
   (both yes and no answers). If the cache exceeds 256 entries it's cleared wholesale.
4. Otherwise `check_admin_token(token)` (indirection for tests; real one is
   `_ask_admin_service`) does `GET {ADMIN_URL}/api/admin/session` with
   `Cookie: am_admin=<token>`, timeout 3 s, in a worker thread. HTTP 200 = admin; anything
   else, including errors and timeouts, = not admin (fails closed).

Consequences: after the admin signs out, QuickShot may still treat the token as admin for up
to 30 s. The token is only forwarded to `ADMIN_URL` (internal Docker network), never logged.

Used by `GET /api/comments/{cid}` (to set `admin` and `can_delete`) and
`DELETE /api/comments/{cid}/{comment}` (to allow deleting others' comments). Nothing else is
admin-gated.

## `X-QuickShot: 1` header (CSRF guard)

`need_header()` (`app.py:226-228`) rejects any state-changing request without
`X-QuickShot: 1` (403). Browsers can't add a custom header to a cross-site request without a
CORS preflight, and QuickShot sends no CORS headers, so another website open on a phone can't
like/comment/delete via that phone (the `am_admin` cookie would otherwise ride along on a
cross-site DELETE). Required on: `POST api/setup`, `POST api/likes/*`, `POST api/views/*`,
`POST api/comments/*`, `DELETE api/comments/*/*`. Not required on the GETs. The page's `H`
constant (`static/index.html:137`) sends it on every change. QuickShot sends `X-Drop: 1` to
Drop for the same reason on Drop's side.

## Input validation and output escaping

- Clip ids must be 16 lowercase hex (`ID_RE`), comment ids 12 hex (`CID_RE`); malformed ids
  are rejected before touching state, which also keeps junk keys out of the JSON files.
- Items from Drop are re-validated too (`ID_RE` on ids, `VIDEO_RE` on type, `isinstance`
  dict), and fields copied into the feed are type-coerced and length-capped (name 200,
  creator 40).
- Comment text and names go through `clean_text` (NFC, control/format characters removed so
  no bidi-override or zero-width tricks, newline runs collapsed, length capped).
- The page escapes every server string with `esc()` before `innerHTML`, and uses
  `textContent` for counts. Captions build `<b>` tags only after escaping.
- Request bodies are Pydantic models (`Like`, `View`, `NewComment`); wrong types -> 422.

## Abuse limits (all soft, since viewer ids are client-chosen)

- Comments: 3 s per viewer id, 300 chars, newest 500 kept per clip.
- Likes: once per viewer id per clip, at most 2000 likers per clip.
- Views: once per viewer id per clip per 30 min.
A determined user on the hotspot can mint new viewer ids to inflate counts or bypass the
comment gap. Accepted: the audience is the people sharing the router.

## Video access

QuickShot doesn't gate video. The browser fetches `../drop/api/files/{id}` directly; Drop
serves `video/*` inline (with a sandbox CSP and `nosniff`) even when downloads are turned off
for that item. QuickShot's only nod to "downloads off" is disabling the context menu on that
`<video>` (`static/index.html:222`), which is cosmetic.

## Container hardening

`Dockerfile`: `python:3.12-slim`, runs as system user `quickshot` (uid 10001); `/data` is
created and chowned in the image so a fresh named volume inherits that owner. No privileges,
no outbound internet needed (only `drop:8000` and `admin:8000` on the stack network).
