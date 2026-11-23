# Testing, dev setups, build, CI and release

## API tests (`tests/test_quickshot.py`)

A plain script, not pytest: `python tests/test_quickshot.py`. Needs `fastapi` (from
`requirements.txt`) plus `httpx` for `TestClient` (CI installs `httpx==0.28.1`). Locally use
the platform's Python or a venv with those installed.

How it works:
- Sets `DATA_DIR` to a fresh temp dir **before** importing `app`, so state files never touch
  `/data`.
- Replaces `qs.drop_call` with `fake_drop`, which serves a fixed `ITEMS` list for
  `GET /api/items` and fakes `POST /api/folders` (records created folders); any other Drop
  call raises.
- Fixture tree: top-level folder `"quickshot "` (lower case + trailing space, testing the
  casefold/strip match) with clip C1 directly in it; `Beach` (C2) and `Beach/Sunsets` (C3,
  `download: false`); a nested folder also named `QuickShot` inside Beach (must be ignored);
  C4 at Drop's top level (outside); a PDF in QuickShot (not a video).
- `__main__` runs every `test_*` function **in definition order** inside one
  `TestClient(qs.app)`. Tests share state on purpose (e.g. the restart test expects the like
  and view counts left by earlier tests), so order matters; don't run them individually or
  shuffled without making them independent.

| Test | Checks |
|---|---|
| `test_page_uses_relative_urls` | The page references `"api/feed`, never `"/api/`, and has no absolute/protocol-relative/root-relative `src`/`href` |
| `test_page_has_home_button` | Exactly one `a.am-home` with `href="../"` and `aria-label="AirplaneMode home"`; the header isn't `hidden` in the markup; the brand links to `./`, not out of the app |
| `test_feed_is_the_quickshot_folder` | `drop_path`, folder id, clip set and order `[C2, C3, C1]` (newest first; no PDF, nothing outside), creators (`None`, `Beach`, `Beach` for the deeper clip), `download` false passthrough, zero counts |
| `test_setup_creates_the_folder_once` | No folder -> `folder: null`; setup without header 403; creates `QuickShot` once; second call creates nothing |
| `test_drop_down` | Drop raising -> 503 with "Drop" in the detail |
| `test_likes_once_per_viewer` | Header required; idempotent like; two viewers = 2; `liked` per viewer in the feed; unlike response; bad viewer and bad clip id -> 400 |
| `test_views_once_per_viewer_per_half_hour` | Dedupe per viewer; another viewer counts; ageing `recent` past `VIEW_GAP` counts again |
| `test_comments` | Cleaning (control char, newline collapse, strip), name newline -> space, oldest-first list, `mine`/`can_delete`, **no viewer id in the JSON**, feed comment count, header 403, empty 400, 400-char 413, rate limit 429, delete others' 403, delete without header 403, delete own -> count, unknown id 404 |
| `test_admin_can_delete_any_comment` | With `ADMIN_URL` set and `check_admin_token` faked: wrong token can't delete; right token sees `admin: true`, `can_delete` on others' comments, and can delete |
| `test_everything_survives_a_restart` | Reset singletons; counts and comments reload from the JSON files |
| `test_a_damaged_file_is_kept_not_overwritten` | A broken `comments.json` gives an empty list and is renamed to exactly one `comments.json.bad-*` with the original bytes |
| `test_old_damaged_copies_are_pruned` | With five `stats.json.bad-*` and one `comments.json.bad-*`, a load keeps the newest 3 stats copies and the comments one |
| `test_deleted_clips_lose_their_counts` | Removing C2 from Drop forgets its views and comments; removing the folder forgets nothing |

Test hooks in `app.py`: `drop_call`, `check_admin_token`, `_admin_cache`, `ADMIN_URL`, the
`stats`/`comments` singletons, `Stats.recent`, `Comments.last` (the helper `post()` clears a
viewer's `last` to skip the cooldown).

Not covered by tests: the page's JavaScript behaviour (ordering, virtualisation, gestures,
refresh). Check those manually in a browser (phone-sized viewport, and a real phone for
autoplay/sound and snap scrolling).

## Dev setups

1. **Standalone** (this repo's `compose.yaml`, project name `quickshot-dev`):
   `docker compose up -d --build` -> QuickShot on `http://localhost:8769` (`PORT`), Drop
   built from `../airplanemode-drop` on `http://localhost:8766` (`DROP_PORT`). QuickShot gets
   `DROP_URL=http://drop:8000` and an absolute `DROP_PATH=http://localhost:8766/`. Named
   volumes `quickshot-data`, `drop-data`. No admin service, so no admin delete here.
   VS Code tasks: "Standalone: up/down", "Tests: QuickShot API" (default test task).
2. **Platform dev stack** (the real test bed): from `../airplanemode`,
   `docker compose -f compose.yaml -f compose.dev.yaml up -d --build` builds this repo as
   `airplanemode-quickshot:dev`; open `http://localhost:8080/quickshot/` (admin password
   `dev` for testing admin deletes). Here `DROP_PATH` is the default `../drop/`, which is the
   path that actually ships.

Port 8000 is reserved by Windows on Jesse's machine; that's why the dev ports are 8769/8766.

## Image

`Dockerfile`: `python:3.12-slim`; install `requirements.txt` (`fastapi==0.115.6`,
`uvicorn==0.34.0`); copy the repo (minus `.dockerignore`: `.git`, `.github`, `.vscode`,
`tests`, `__pycache__`, `*.md`, `compose.yaml`, `docs/screenshots`, `docs/icon.svg`); create
user `quickshot` uid 10001 and `/data` owned by it; `VOLUME /data`; `EXPOSE 8000`;
`uvicorn app:app --host 0.0.0.0 --port 8000`. Memory budget 64m (no video passes through).

`.gitattributes` forces LF for `*.sh`, `*.yaml`, `*.yml`, `Dockerfile` (git has autocrlf on
on Windows; these run on Linux).

## CI (`.github/workflows/image.yml`)

Triggers: push to `main`, tags `v*`, pull requests. Job `build` on ubuntu-latest:
1. checkout, Python 3.12;
2. **API tests**: `pip install -r requirements.txt httpx==0.28.1` then
   `python tests/test_quickshot.py` (fails the job on any assert);
3. QEMU + Buildx; GHCR login (not on PRs);
4. `docker/metadata-action`: tags `type=ref,event=branch` (-> `:main`) and
   `type=semver,pattern={{version}}` (tag `v1.2.3` -> `:1.2.3`);
5. build `linux/arm64,linux/amd64` (arm64 for the NanoPi R6S), push unless it's a PR.
Image: `ghcr.io/jessewolcott/airplanemode-quickshot`.

## Releasing

1. Make sure CI on `main` is green.
2. **Ask Jesse first**, then push a `vX.Y.Z` tag. CI tests and publishes `:X.Y.Z`.
3. In the platform repo, bump the pinned tag in `compose.yaml`
   (`ghcr.io/jessewolcott/airplanemode-quickshot:X.Y.Z`; never `latest`). The Caddy route
   and `portal/apps.json` entry already exist.
Released so far: 0.1.0, 0.1.1 (home button).

## Hardware status

Nothing has been tested on real hardware (the R6S build is in progress). Things to check on the
device: smoothness of snap scrolling and buffering of the +/-2 window over Wi-Fi, how many
simultaneous streams Drop sustains, memory under 64m with large `comments.json`, and
autoplay/sound behaviour on iOS Safari and Android Chrome. Keep the platform's
`docs/testing.md` checklist current.
