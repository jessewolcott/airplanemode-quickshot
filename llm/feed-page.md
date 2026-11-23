# The feed page (`static/index.html`)

One self-contained file: CSS (`:10-108`), a tiny DOM skeleton (`:112-117`: `header#top`,
`main#feed`, `div#empty`) and one inline script (`:119-446`). No framework, no build step,
no external resources (fonts, scripts and icons are all inline; the favicon is a data: URI).
All URLs are relative (`api/feed`, `../`, `drop_path + "api/files/"`), because the platform
serves the app under `/quickshot/` (a test enforces this).

## Global state (`:133-144`)

| Name | Meaning |
|---|---|
| `mem` | `localStorage` wrapper, keys prefixed `qs.`, JSON-encoded, all errors swallowed |
| `viewer` | Anonymous id: `qs.viewer`, else 20 random chars from `[a-z0-9]` via `crypto.getRandomValues` (fits the server's `VIEWER_RE`). If storage is unavailable, a fresh id each page load |
| `myName` | `qs.name`, default `traveler-NNNN`; written back immediately |
| `seen` | `Set` of clip ids this device has watched for 2 s (`qs.seen`, last 2000 kept) |
| `data`, `byId` | Last feed response and `id -> clip` map; `index(d)` sets both (its comment about "folder path" is stale: it only indexes) |
| `order`, `slides` | Parallel arrays: the play order (clip objects) and their `<section>` elements |
| `active` | Index of the slide on screen (-1 before any) |
| `unmuted` | Whether the user turned sound on |

## Helpers (`:146-155`)

- `hue(s)`/`tint(s)`: string hash -> `hsl(h 75% 72%)`, used for creator avatars and commenter
  avatars (stable colour per name).
- `count(n)`: `999`, `1.2K`, `12K` (>= 10,000 rounds to whole K), `1.2M`.
- `ago(t)`: `now`, `5m`, `3h`, `2d` from an epoch-seconds timestamp.
- `caption(name)`: strips a 2-5 character extension, turns `_` into spaces, collapses spaces,
  HTML-escapes, then bolds `#tags` (Unicode letters/digits/underscore after start-or-space).
- `creator(c)`: `c.creator` or `"quickshot"`, lower-cased, keeping only letters, digits, `_`
  and `.`; falls back to `quickshot` if that leaves nothing. Shown as `@handle`.
- `esc()` is applied to every server string put into `innerHTML`.

## Ordering: never runs out (`:157-169`)

`nextRound(first)` returns one full pass over `data.clips`:
- **First round:** clips not in `seen`, in the server's order (newest first), followed by the
  seen ones shuffled (Fisher-Yates).
- **Later rounds:** all clips shuffled, then a stable sort pushes the last
  `min(3, floor(n/2))` clips of `order` (the ones just shown) to the end of the round, so the
  seam between rounds doesn't repeat what you just watched.
  (With a single clip, `slice(-0)` returns the whole order, so everything is "recent" and
  the sort is a no-op; harmless.)

`addRound(first)` (`:224-231`) appends a slide per clip to `order`, `slides`, the DOM, and the
IntersectionObserver. `activate()` calls `addRound(false)` when you reach any of the last 3
slides (`:258`), so the feed is endless. Slides are never removed; only their `<video>` is.

## Slides and virtualisation (`:171-223`)

- `makeSlide(c, i)` builds a `<section class="slide" data-i=i>` containing a `.frame` with a
  spinner placeholder (`.ph`), top/bottom gradient shades, the meta block (`@creator · age`,
  caption clamped to 3 lines, view count), the right-hand rail (creator avatar, like,
  comments, sound buttons) and a progress bar. It stores the clip on `s.clip` and calls
  `paint(s)`. **No `<video>` yet.**
- `paint(s)` re-renders the dynamic bits from the current `byId` entry: liked state (heart
  filled + pop animation via `.liked`), like count, comment count, sound icon/label
  (On/Off), `N view(s)`.
- `attach(s)` lazily creates the `<video>`: `playsInline`, `loop`, `muted = !unmuted`,
  `preload = "auto"`, `disablepictureinpicture`; `oncontextmenu` returns false when the clip's
  `download` is false (cosmetic only: it's still streamable); `src = data.drop_path +
  "api/files/" + id`. On `loadedmetadata` it adds class `cover` when the clip is portrait
  (`videoHeight > videoWidth * 1.1`): portrait fills the frame (`object-fit: cover`),
  landscape/square fits with bars (`contain`). `playing` removes the spinner; `timeupdate`
  drives the progress bar.
- `detach(s)` pauses, removes `src`, calls `load()` (this really releases the network
  connection and decoder; just removing the element can leave it buffering), removes the
  element, and puts the spinner back.

**Window rule** (`activate`, `:242-246`): slides within **+/-2** of the active index have a
video (neighbours attached and paused, so with `preload=auto` the next clips buffer ahead);
every other slide is detached. So at most 5 `<video>` elements exist (rule in CLAUDE.md).
The window is by slide index, so hidden ("gone") slides still count toward it.

## Which slide is active (`:233-259`)

- One `IntersectionObserver` with threshold 0.6 on every slide; when a slide is >= 60%
  visible, `activate(i)`.
- `#feed` is a fixed full-screen scroller with `scroll-snap-type: y mandatory`; each slide is
  `100dvh`, `scroll-snap-align: start`, `scroll-snap-stop: always` (one clip per swipe, no
  flinging past several).
- `activate(i)`: no-op if already active; clears the pending view timer; applies the window
  rule and removes any pause overlay; then for the active slide sets `muted = !unmuted`,
  **`currentTime = 0`** (returning to a clip restarts it) and `play()`.

### Autoplay / mute policy
Browsers only allow autoplay without a user gesture when muted. So videos start muted and the
header shows a pulsing white **"Tap for sound"** chip (`#sound`, hidden in empty states).
- `play()` rejection fallback: if the video was unmuted (sound on), retry muted. Note that
  `unmuted` stays true in that case, so the rail still says "On" while that clip is muted.
- `setSound(on)` (`:305-311`): sets `unmuted`, hides the chip once sound is on (the chip only
  ever turns sound on), unmutes/plays the active video inside the tap gesture, updates
  `muted` on the other attached videos and repaints every slide. The rail's sound button and
  the `m` key toggle it. Once on, it stays on for the rest of the session (new videos are
  created with `muted = !unmuted`).

### View timer (`:253-257`)
After **2 s** on the active slide: add the clip to `seen` (persist the last 2000), then
`POST api/views/{id} {viewer}`; on reply update `views` in `byId` and repaint every slide of
that clip. Errors ignored. Swiping away within 2 s cancels it. The server separately dedupes
to once per 30 min.

## Taps and the heart burst (`:261-302`)

One delegated `click` listener on `#feed`:
- Rail buttons first: `[data-like]` -> `toggleLike`, `[data-comments]` -> `openComments`,
  `[data-mute]` -> `setSound(!unmuted)`. Other clicks on `.rail` or `.meta a` are ignored.
- Otherwise tap logic with a **280 ms** window: the first tap records `lastTap` and schedules
  `togglePause(s)` 280 ms later. A second tap inside the window cancels that timer (so a
  double-tap doesn't pause), shows a burst, and likes the clip **only if not already liked**
  (double-tap never unlikes).
- `togglePause(s)` plays/pauses the slide's video and shows a centred play icon overlay while
  paused.
- `burst(s, x, y)` drops a filled heart at the tap point with a random +/-20 deg tilt
  (`--r`), animated 0.8 s (scale pop, float up, fade), removed after 850 ms.

### Likes: optimistic update (`toggleLike`, `:293-302`)
Flip `liked`, adjust `likes` by +/-1, repaint all slides showing that clip (the same clip
appears once per round), then `POST api/likes/{id} {viewer, liked}`. If the response has
`likes`, adopt the server's `likes`/`liked` ("the box has the final word"). On a network
error, revert. (A non-OK JSON response without `likes`, e.g. 400, leaves the optimistic
value.) Repaint again.

## Comments sheet (`:314-371`)

- `openComments(id)`: sets `sheetFor`, quietly closes any open sheet, appends a backdrop
  (`.sheet-bg`, click closes) and a bottom sheet (`role="dialog"`, `min(100vw,560px)` wide,
  `min(70dvh,620px)` tall) with title, list, error line, and a form: your name button,
  auto-growing textarea (`maxlength=300`, up to 110 px tall, `enterkeyhint="send"`) and Post.
- Enter sends, Shift+Enter makes a newline. Post is disabled while the text is blank.
- Submit: `POST api/comments/{id} {viewer, name: myName, text}`; on error shows the server's
  `detail` (e.g. "Slow down a little.") in `.cerr`; on success clears the box, updates the
  clip's comment count everywhere (`setCount`) and reloads the list scrolled to the end.
- `loadComments(id, toEnd)`: fetches `api/comments/{id}?viewer=`, ignores the result if the
  sheet was closed or switched; title shows `N comment(s)`; each comment shows a tinted
  initial avatar, name, ` (you)` when `mine`, age, text (`pre-wrap`), and a **delete** button
  when `can_delete` (own comments, or all of them for the admin). Empty list: "No comments
  yet. Say something."
- Delete: `confirm()`, then `DELETE api/comments/{id}/{cid}?viewer=`; refresh count and list,
  or show the error.
- While a sheet is open, the list is re-polled **every 8 s** (`:371`) so other people's
  comments appear.
- **Name change:** the name button opens `prompt()`; a non-blank answer is trimmed, cut to 24
  characters, saved to `qs.name`, and shown on the button. Affects future comments only.
- `closeComments(quiet)`: removes sheet and backdrop; non-quiet also clears `sheetFor`.

## Keyboard (`:373-384`)

Inside a textarea/input only `Escape` (close sheet) is handled. Otherwise:
`ArrowDown`/`j` next clip, `ArrowUp`/`k` previous (smooth `scrollIntoView`, snapping then
triggers activation), `Space` pause/play, `m` sound, `l` like toggle, `c` open comments,
`Escape` close comments.

## Loading, empty states, refresh (`:386-445`)

- `fetchFeed()`: `GET api/feed?viewer=` with `cache: "no-store"`; throws the server `detail`
  on non-OK.
- `start()`: on error -> `showEmpty(null, message)`; no clips -> `showEmpty(d)`; otherwise
  hide the empty screen, show the header, index, reset `order/slides/active`, clear `#feed`,
  `addRound(true)`, scroll to top. Called at load and by the empty-state buttons.
- `showEmpty(d, err)` (hides the header) has three variants:
  1. **Error** (e.g. Drop down, 503): "Can't load the feed", the message, **Try again**.
  2. **No folder** (`d.folder === null`): explains the feed comes from Drop's folder named
     `folder_name`, which doesn't exist yet; **Create the QuickShot folder** posts
     `api/setup` and restarts, or toasts "Couldn't create it. Is Drop running?".
  3. **Folder but no clips**: tells you to put short videos in it (subfolders become
     creators, tap **keep** so they don't expire); **Open QuickShot in Drop** links to
     `drop_path + "#f=" + folder.id` (Drop's page opens that folder from the hash), plus
     **Refresh**.
- `toast(t)`: white pill at the top for 2.6 s.

### 30 s refresh (`:425-443`)
Every 30 s, if the feed has loaded and the tab is visible:
- re-fetch (errors and an empty clip list are ignored, so a Drop hiccup doesn't blank the feed);
- `fresh` = clips not in `byId`; `gone` = ids in `byId` missing from the new list;
- re-index, swap each slide's `clip` for the fresh object and repaint (counts update);
- gone clips: every slide of theirs except the active one gets `display: none` and is
  detached (they stay in `slides`; future rounds won't include them since `data` changed);
- fresh clips: inserted in the DOM **right before `slides[active + 1]`**, i.e. right after
  the clip being watched; then `slides`/`order` are rebuilt from DOM order, renumbered
  (`data-i`) and observed; toast "1 new clip coming up" / "N new clips coming up".

## Layout and styling

- Dark only (`color-scheme: dark`), black background, accent `#9b7bff` (violet), heart
  `#ff4d6d`. Its own look and bolt logo; not any real app's branding.
- `--col: min(100vw, calc(100dvh * 9 / 16))`: the content column is 9:16. On a phone it's the
  full width; on a wide screen it's a centred portrait column. At `min-width: 700px` the
  frame gets rounded corners, a 24 px vertical inset and a hairline ring. The header is fixed
  at the column's width and centred.
- `100dvh` and `env(safe-area-inset-*)` handle mobile browser bars and notches;
  `viewport-fit=cover`, `interactive-widget=resizes-content` (the keyboard resizes the page
  so the comment box stays visible).
- `overscroll-behavior: none/contain` stops pull-to-refresh and scroll chaining.
- `prefers-reduced-motion: reduce` disables the sound-chip nudge, burst and like pop.
- Brand link goes to `../` (the AirplaneMode landing page).
