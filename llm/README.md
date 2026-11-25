# QuickShot: how it's built (LLM / engineer reference)

Checked-in notes (`.dockerignore` lists `llm`, so none of this reaches the image). Written from the code as of release 0.1.1 (2026-11-22). If the code
and these notes disagree, the code wins; fix the notes.

## Overview

QuickShot is an AirplaneMode app: a full-screen, endless, vertical short-video feed (swipe up
for the next clip) for every phone on the travel router's hotspot, with no internet. It owns
**no video files**. The clips are the `video/*` files in Drop's top-level folder named
`QuickShot` (any capitalisation, subfolders included; the subfolder directly under QuickShot
is shown as the clip's "creator", e.g. `QuickShot/Beach/x.mp4` -> `@beach`). The backend
(`app.py`, FastAPI, ~430 lines) asks Drop for its item list and returns the feed; the browser
then streams each video **straight from Drop** (`../drop/api/files/{id}`), so QuickShot never
handles video bytes and runs in 64 MB. What QuickShot does own is the shared social state:
likes (once per viewer), views (once per viewer per clip per 30 minutes) and comments, kept in
two JSON files in its `/data` volume. There are no accounts: each browser invents a private
random viewer id. The AirplaneMode admin (recognised via the platform's `am_admin` session
cookie) can delete any comment. Putting clips in, and creating the folder, are writes to
Drop, which on the box only the admin may do unless the admin opened Drop up (platform
decision 0026); QuickShot's setup forwards the caller's `am_admin` cookie to Drop for that. The whole UI is one file, `static/index.html` (~450 lines of
HTML/CSS/vanilla JS).

## Files in this folder

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | The pieces, request flow, how it sits in the platform, repo layout, config |
| [backend.md](backend.md) | `app.py` in depth: Drop access, folder/clip discovery, creators, every endpoint |
| [social-state.md](social-state.md) | Likes, views, comments: data model, rules, persistence, locks, forgetting |
| [feed-page.md](feed-page.md) | `static/index.html` in depth: ordering, virtualisation, playback, gestures, comments sheet, refresh, empty states, layout |
| [security-and-privacy.md](security-and-privacy.md) | Viewer-id privacy, admin recognition, `X-QuickShot` header, input validation, trust model |
| [testing-and-release.md](testing-and-release.md) | Tests, dev setups, Docker image, CI, releasing, platform wiring |
| [gotchas-and-extending.md](gotchas-and-extending.md) | Non-obvious behaviour, known quirks, and how to add things safely |

## Source map

| Path | Role |
|---|---|
| `app.py` | The whole backend |
| `static/index.html` | The whole frontend |
| `tests/test_quickshot.py` | API tests (plain script, no pytest) |
| `Dockerfile`, `requirements.txt`, `.dockerignore` | Image |
| `compose.yaml` | Standalone dev (QuickShot + Drop built from `../airplanemode-drop`) |
| `app.yaml` | AirplaneMode app manifest |
| `.github/workflows/image.yml` | CI: tests, then multi-arch image to GHCR |
| `CLAUDE.md`, `README.md` | Rules and user-facing description |
| `../airplanemode/docs/decisions/0014-quickshot.md` | The design decision |

## The rules that must not break (from `CLAUDE.md`)

1. Never proxy video; the browser streams from Drop. Memory limit 64m.
2. `comments.json` / `stats.json` in `/data` are persistent; write them atomically under their locks.
3. Viewer ids are never returned in any response (tests check).
4. Only forget counts for deleted clips when the QuickShot folder was found.
5. Admin = platform admin session (`am_admin` cookie checked at `ADMIN_URL/api/admin/session`).
6. Page uses relative URLs only (a test checks); at most current +/- 2 `<video>` elements.
7. Keep the look its own; it's a generic feed, not a copy of any particular app's branding.
