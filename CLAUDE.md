# QuickShot (AirplaneMode app): notes for Claude

An endless vertical feed of the videos in Drop's top-level "QuickShot" folder, with shared
likes, views and comments. FastAPI app in `app.py`, one page in `static/index.html`.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes and `docs/app-contract.md` for the rules. The clips are Drop's
(`../airplanemode-drop`); Cinema (`../airplanemode-cinema`) is the sibling that reads Drop
the same way.

## Commands

```powershell
docker compose up -d --build         # standalone dev: QuickShot :8769 + Drop :8766
python tests/test_quickshot.py       # API tests (CI runs these before building)
```

The real test bed is the platform dev stack (compose.dev.yaml builds this repo).

## Rules

- **Never proxy video.** The browser streams from Drop. Memory limit 64m.
- **Comments/likes/views are persistent state** in `/data` (a named volume). Write
  `comments.json` / `stats.json` atomically under their locks. Don't move them anywhere the
  container's filesystem would lose them.
- **Viewer ids are private**: never return them in any response (tests check). Responses carry
  `mine` / `liked` / `can_delete` computed for the caller.
- Only forget counts for deleted clips when the QuickShot folder was found (a missing folder
  must not wipe everything).
- Admin = platform admin session (`am_admin` cookie checked at `ADMIN_URL/api/admin/session`,
  same as Drop). The admin can delete any comment.
- Page: relative URLs only (a test checks); at most current±2 `<video>` elements.
- Keep the look its own (name, colours, icons); it's a generic short-video feed, not a copy
  of any particular app's branding.

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-quickshot:X.Y.Z`
(arm64 + amd64). **Ask Jesse before pushing a release tag.** Released: 0.1.0, 0.1.1 (home button), 0.1.2 (prunes old `.bad-*` copies).
