# Footage Archive — Project Status

Personal media management tool for cataloguing travel footage (photos & videos).
Runs on an Unraid NAS server, edited over a 5Gbit network.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn |
| Database | PostgreSQL via SQLAlchemy Core 2.0 (psycopg2 driver) |
| DB migrations | Alembic |
| Package manager | uv (pyproject.toml + uv.lock) |
| Frontend | Angular 21.2, TypeScript 5.9 |
| Maps | Google Maps (`@angular/google-maps`): map view, detail/location maps, geocoding — see `GOOGLE_SETUP.md` |
| Metadata extraction | exiftool (all photo EXIF + full-dump endpoint) |
| Preview generation | FFmpeg + FFprobe + Pillow + rawpy |
| PDF generation | reportlab (list card export, base-14 fonts only — no system TTFs) |
| Containerisation | Docker + Docker Compose (backend + frontend; linux/amd64 for Unraid) |
| Frontend serving | nginx (serves static Angular bundle + reverse-proxies `/api` to the backend) |

---

## Running Locally

**Prerequisites:**

FFmpeg (provides `ffmpeg` + `ffprobe`, required for video probing and clip previews):
```bash
brew install ffmpeg
```

exiftool (required for all photo EXIF extraction and the full-metadata endpoint):
```bash
brew install exiftool
```

A reachable PostgreSQL instance. For dev, create the roles + database and grant privileges using the scripts in `dbeaver/dev/` (run as a superuser; substitute the `${...}` password placeholders):
```
dbeaver/dev/create_users_and_db.sql   # owner + app roles, database, CONNECT grant
dbeaver/dev/grant_privileges.sql      # schema ownership + default table/sequence grants for the app role
```

**Backend:**
```bash
# from project root
uv sync
uv run python app.py
# On startup app.py runs `alembic upgrade head` to bring the schema up to date.
# API available at http://localhost:8051
# Swagger UI at http://localhost:8051/docs
```

**Frontend:**
```bash
cd frontend
npm install
npx ng serve
# available at http://localhost:4200
```

**.env file** (project root, gitignored):
```
# DB_URL holds the driver/host/port/database; the username + password are injected
# from the separate vars below (see env/environment.py::get_database_url).
DB_URL=postgresql://192.168.2.230:5432/footage_archive_dev
DB_USER=footage_archive_dev_app           # app role: SELECT/INSERT/UPDATE/DELETE
DB_PASSWORD=...
DB_OWNER_USER=footage_archive_dev_owner   # owner role: used by Alembic for DDL
DB_OWNER_PASSWORD=...
ROOT_DIR=./footage
MEDIA_TYPE_VIDEO=.mov,.mp4
MEDIA_TYPE_PHOTO=.jpg,.jpeg,.rw2
MEDIA_TYPE_360_VIDEO=.insv
MEDIA_TYPE_360_PHOTO=.insp,.dng
BROWSER_HIDDEN_EXTENSIONS=.xmp,.acr,.psd,.lrv,.identifier
# Google Maps (served to the frontend via /config). Blank = maps disabled.
# How to obtain these: see GOOGLE_SETUP.md.
GOOGLE_MAPS_API_KEY=...
GOOGLE_MAPS_MAP_ID=...
```

The app connects as `DB_USER` (DML only); Alembic connects as `DB_OWNER_USER` (DDL). See `alembic/env.py`.

**Test footage** lives in `footage/` (gitignored):
```
footage/japan_2024/
├── video/atami/{beach,castle,shopping_street,station}/   (MOV, Lumix)
├── video/nara/                                           (MOV, Lumix)
├── photo/atami/ + photo/kyudo/ + photo/sap_badge_shooting/  (JPG+RW2, Lumix)
├── 360/nagasaki/                                         (INSV+INSP+DNG, Insta360)
└── phone/kokura/{photo,video}/                           (JPG+MP4, Samsung)
```

---

## Running with Docker Compose

The full stack (backend + frontend) runs as a Compose stack. **PostgreSQL is external** (e.g. on the NAS) — Compose does not run a database; point `DB_URL` at the existing instance.

```bash
cp .env.example .env          # fill in DB creds, set FOOTAGE_DIR + FRONTEND_PORT
docker compose up -d --build
# → app at http://<host>:${FRONTEND_PORT:-8080}
docker compose down           # stop
```

Two services (`docker-compose.yml`):
- **`backend`** — built from the root `Dockerfile`; reads env from `.env`; `ROOT_DIR` is forced to `/footage` and the host `${FOOTAGE_DIR}` is mounted there. Port `8051` is internal-only (`expose`), not published — uncomment the `ports:` block to reach Swagger/the API directly for debugging.
- **`frontend`** — built from `frontend/Dockerfile` (multi-stage: Node builds the Angular prod bundle → nginx serves it). The **only published service** (`${FRONTEND_PORT:-8080}:80`).

**Single-origin design:** nginx (`frontend/nginx.conf`) serves the static SPA *and* reverse-proxies `/api/` → `backend:8051/` (the trailing slash strips the `/api` prefix, so `/api/config` → backend `/config`). The browser only ever talks to nginx, so there's no hardcoded backend host and no CORS needed. The production Angular build swaps in `environment.production.ts` (`apiUrl: '/api'`, relative) via `fileReplacements` in `angular.json`; the dev `ng serve` flow is unchanged (`environment.ts` → `http://localhost:8051`).

```
browser ──▶ frontend (nginx :8080) ──┬─▶ static Angular bundle
                                      └─▶ /api/* ──▶ backend:8051 ──▶ external Postgres
```

---

## Versioning

Both images carry a version that is visible at runtime in the bottom of the frontend sidebar (**Frontend** = the bundle's own version, **Backend** = fetched live from `GET /version`).

**Single source of truth = the git tag.** CI (`docker/metadata-action`) already derives a semver from `v*.*.*` tags and stamps it onto each image (tag + OCI labels). On top of that, both workflows pass the version into the build as the `APP_VERSION` build-arg:
- **Backend** — `APP_VERSION` is set as an `ENV` in the `Dockerfile`; `Environment.get_version()` reads it. Local-dev fallback: the `version` in `pyproject.toml`. `GET /version` returns `{"version": ...}`.
- **Frontend** — the `prebuild` npm hook (`scripts/generate-version.mjs`) reads `APP_VERSION` and writes `src/version.ts`, which `app.component.ts` imports. Local-dev fallback: the `version` in `package.json`.

A tag push (`v1.2.3`) reports that semver; a plain `main` push reports the short commit SHA.

**To cut a release — bump all three in lockstep, then tag:**
1. `pyproject.toml` → `version`
2. `frontend/package.json` → `version`
3. `frontend/src/version.ts` → `APP_VERSION` (keep it equal to `package.json`, so local builds produce no git churn)
4. Commit, then `git tag vX.Y.Z && git push --tags` (this is what makes the published image report `vX.Y.Z`).

Keep these in sync with the git tag. The committed `version.ts` value is only the local-dev display; CI overwrites it from the tag at build time.

---

## Deploying to Unraid

> Legacy single-image flow (backend only). The Compose stack above is the current full-stack path.

```bash
# build and export
./build_for_unraid.sh        # produces footage-archive-unraid.tar on Desktop

# on Unraid
./load-footage-archive.sh    # docker load
./run-footage-archive.sh     # docker run (mounts /mnt/user/backup/)
```

Docker env vars to set on Unraid:
- `DB_URL=postgresql://<host>:5432/footage_archive`
- `DB_USER=...` / `DB_PASSWORD=...` (app role)
- `DB_OWNER_USER=...` / `DB_OWNER_PASSWORD=...` (owner role, used for Alembic migrations on startup)
- `ROOT_DIR=/mnt/user/footage`
- `MEDIA_TYPE_VIDEO=.mov,.mp4`
- `MEDIA_TYPE_PHOTO=.jpg,.jpeg,.rw2`
- `MEDIA_TYPE_360_VIDEO=.insv`
- `MEDIA_TYPE_360_PHOTO=.insp,.dng`
- `BROWSER_HIDDEN_EXTENSIONS=.xmp,.acr,.psd,.lrv,.identifier`
- `GOOGLE_MAPS_API_KEY=...` / `GOOGLE_MAPS_MAP_ID=...` (optional; enable the maps — see `GOOGLE_SETUP.md`)
- `TASK_POLL_INTERVAL_MS=5000` (default, optional)
- `WORKER_POOL_SIZE=4` (default, optional) — shared worker-pool size for in-job parallel hashing/probing
- `DB_POOL_SIZE=5` / `DB_MAX_OVERFLOW=10` (defaults, optional) — SQLAlchemy connection pool; max concurrent connections = sum of the two

---

## Architecture

```
footage-archive/
├── docker-compose.yml      # Full stack: backend + frontend (external Postgres)
├── Dockerfile              # Backend image (python:3.13-slim + ffmpeg + exiftool + uv)
├── .env.example            # Template for .env (DB creds, media types, compose vars)
├── app.py                  # FastAPI entry point, lifespan, CORS, DB init
├── api/
│   ├── base.py             # GET / (redirect to /docs), GET /version
│   ├── config.py           # GET /config  ← root_dir, task_poll_interval_ms, google_maps_api_key, google_maps_map_id
│   ├── files.py            # POST /files/directory, GET /files/details, GET /files/exif (full exiftool dump), PATCH /files/rename (file or directory, routed through fileops/), POST /files/move + /files/move/preview, POST /files/mkdir, GET /files/clip-preview/{md5_hash}, PATCH /files/location, POST /files/checksum
│   ├── search.py           # GET /files/search-facets (facet autocomplete), POST /files/search (filtered, paginated search; incl. list_ids + list_code)
│   ├── keywords.py         # GET /keywords (all), POST /keywords (add to file), DELETE /keywords (remove from file)
│   ├── lists.py            # GET/POST /lists, PATCH/DELETE /lists/{id}, GET/POST /lists/{id}/items, DELETE /lists/{id}/items/{md5_hash}, GET /lists/{id}/items/by-code/{code}, GET /lists/{id}/export.pdf (cut-out cards, cols/rows query params)
│   ├── locations.py        # GET /locations, POST /locations (create), GET /locations/map-points (clustered map markers)
│   ├── tracking.py         # POST /tracking/scan-directory, /scan-file, /import-metadata, /rediscover (MD5-based reconciliation of a moved folder, backed by fileops/rediscover.py); GET /tracking/conflicts(/count) + POST /tracking/conflicts/resolve(-batch) (#25, path-conflict decisions left behind by a rediscover)
│   ├── ai.py               # POST /ai/classify-shot — ML shot-type classification for a tracked video
│   ├── tasks.py            # GET /tasks, GET /tasks/{id}, DELETE /tasks/completed, DELETE /tasks/{id}
│   ├── troubleshoot.py     # GET /trouble-shooting/missing-preview, POST /trouble-shooting/missing-preview/fix, GET /trouble-shooting/missing-files (optional ?path= subtree)
│   └── dtos.py             # Pydantic request/response models (search query/results, etc.)
├── exports/
│   └── list_cards_pdf.py   # Pure PDF renderer (no DB access): A4 grid of cut-out cards for a list — big bold item code, small grey truncated/wrapped relative path, faint shared grid lines, page footer
├── fileops/                # Safe move/rename/mkdir service backing api/files.py's PATCH /rename, POST /move(/preview), POST /mkdir
│   ├── pathlocks.py        # Process-wide in-memory reader/writer path lock registry: shared(path) (scans/tracking block while an overlapping move is in flight) + try_exclusive(paths) (move/rename raises PathLockedError immediately, never waits, on any overlapping shared/exclusive lock); overlap is Path.is_relative_to on resolved paths, never string startswith
│   ├── service.py          # Validation (ROOT_DIR, exists/absent, no self-move, name sanity) + os.rename-only physical moves/renames with sidecar handling + DB path updates, each physical rename journaled in FileOperations before it is attempted; recover_pending_operations() reconciles any 'pending' row left by a crash
│   └── rediscover.py       # MD5-based reconciliation for a rediscover-scan (reusable by a future normal-scan path): pure classify() decides unchanged/relink/conflict/new per hash; apply() relinks Files in one transaction, persists conflicts to PathConflicts (ON CONFLICT DO NOTHING), optionally tracks new hashes via a caller-supplied callback, then prunes stale conflicts. Never touches metadata tables or disk.
├── db/                     # Decoupled DB layer (the only place that knows about SQLAlchemy)
│   ├── engine.py           # Lazy singleton engine (pool_pre_ping) + dialect-aware upsert/upsert_ignore helpers
│   ├── models.py           # SQLAlchemy Core Table definitions (metadata) + indexes — single source of truth for the schema
│   └── database.py         # Database class: all queries/upserts via SQLAlchemy Core, pandas only for DataFrame I/O
├── alembic/                # Schema migrations (Alembic)
│   ├── env.py              # Wires target_metadata = db.models.metadata, connects as DB_OWNER_USER
│   └── versions/           # Migration scripts (0001_initial_schema.py = full baseline)
├── alembic.ini             # Alembic config (script_location, file_template, logging)
├── dbeaver/dev/            # One-off SQL to provision the dev Postgres (roles, db, grants)
├── scanner/scanner.py      # recursive dir walk + MD5 hashing, media_type assignment
├── ffmpeg/ffmpeg.py        # FFprobe (full stream info → VideoProbeResult) + clip preview
├── photos/exif.py          # exiftool EXIF extraction → PhotoProbeResult (all photo formats); full-tag dump_all_exif(); Pillow/rawpy thumbnail generation
├── davinci/davinciresolve.py  # DaVinci Resolve CSV metadata parser
├── shot_classifier/classifier.py  # ML shot-type classifier (backs POST /ai/classify-shot)
├── tasks/taskmanager.py    # in-memory singleton background task queue
├── env/environment.py      # env var reader with fallbacks; builds DB URLs from DB_URL + DB_USER/DB_OWNER_USER
├── sql/                    # LEGACY raw-SQL files (setup.sql etc.) — superseded by Alembic + db/models.py, no longer loaded
└── frontend/               # Angular 21 app
    ├── Dockerfile          # Multi-stage: Node builds the prod bundle → nginx serves it
    ├── nginx.conf          # Serves SPA (try_files fallback) + reverse-proxies /api → backend:8051
    ├── src/environments/
    │   ├── environment.ts             # dev: apiUrl http://localhost:8051
    │   └── environment.production.ts  # prod: apiUrl /api (swapped in via angular.json fileReplacements)
    └── src/app/
        ├── app.component.*         # shell (#38): 72px left rail (brand, Browse/Search/Lists/Map/Health/Settings, version) that
        │                           #   becomes a bottom tab bar under 760px ("More" opens a `MenuComponent` with Health/Settings);
        │                           #   52px topbar with a breadcrumb slot (see `services/header.service.ts`), `app-quick-jump`,
        │                           #   `app-tasks-widget`
        ├── app.routes.ts           # lazy-loaded routes
        ├── app.config.ts           # provideRouter + provideHttpClient
        ├── models.ts               # TypeScript interfaces
        ├── services/api.service.ts # HTTP calls + taskRefresh$ subject
        ├── services/theme.service.ts # Dark/light theme (signal-based, localStorage `fa-theme`, resolves "system" via matchMedia)
        ├── services/header.service.ts # `HeaderService` (#38, signal-based, `providedIn: 'root'`) — lets the active page drive the
        │                           #   topbar's breadcrumb slot: `setCrumbs([{label, action?}, …])` (last item has no action, renders
        │                           #   bold/current) or `setTitle(title)` for a plain one-item trail. `AppComponent` clears it to `null`
        │                           #   on every `NavigationStart` (before the next page's `ngOnInit` can set its own) and falls back to
        │                           #   the route's `title` (`app.routes.ts`) whenever nothing is published. The browser page is the one
        │                           #   publisher so far, via an `effect()` over its existing `breadcrumbs` computed — Scan/Select stay
        │                           #   local to the page (#39 folds them into a proper toolbar)
        ├── tasks-widget/           # Header task indicator (#38): icon button with an SVG progress ring while tasks run (ring = average
        │                           #   fraction parsed from running tasks' `progress` text via `/(\d+)\s*\/\s*(\d+)/`, spins indeterminately
        │                           #   if none match), a small red dot when any task FAILED, and the running/failed count badge; popover
        │                           #   (`PopoverComponent`) lists each task (name, mono start time, description, progress bar, status line,
        │                           #   error, "N conflicts to review →"); "Clear finished" removes COMPLETED tasks via `DELETE /tasks/completed`
        │                           #   and FAILED ones individually (there's no bulk endpoint for those) — running/queued tasks are left alone
        ├── browser/                # Browser page: directory navigator + file detail panel. Toolbar (#39) below the shell topbar:
        │                           #   `.seg`/`.seg-btn` filter (All/Videos/Stills/Untracked, counts from `counts`, zero-count segments
        │                           #   hidden except All, selecting one reloads via `kind`), thumbnail-size slider (`--thumb`, 140–320px,
        │                           #   `localStorage` `fa-thumb`), "Scan folder" (`.btn-ghost`), "Select" (`.btn`/`.btn-on`). Folders render
        │                           #   as `.folder` tiles (icon, name, `file_count`). Section headings use `counts`, not the loaded page
        │                           #   length. Grid/video-grid cards are `shared/media-card/` (ext-badge shown only when the loaded photos
        │                           #   mix formats); the inline rename `<input>`/save/cancel are projected into the card's slot
        │   └── (context menu)      # #41: no own component anymore — the browser builds `MenuItem[]` (`menuItemsFor`) for `shared/menu/`:
        │                           #   header (thumb, name, "Still · JPG" / "Video · MOV · 00:12" / "Folder · N files"), Open, Track,
        │                           #   Add keyword… / Add to list… (popover at the tile), Rename, Move to…, Copy path; folders: Scan,
        │                           #   Rediscover. Opens on right-click or the card's "⋯". Same ids = single-key shortcuts on the
        │                           #   focused tile (Space, T, K, L, F2, M), via `data-path` on cards/folder tiles
        ├── search/                 # Faceted search page (#44): filter rail (toggle chips, facet inputs) + result header with
        │                           #   active-filter chips / Clear all + results grid + sliding detail panel
        ├── map/                    # Map page: Google Maps clustering, flyouts, "open in search"
        ├── lists/                  # Lists feature: overview (create/rename/delete) + list detail (item grid, code jump, remove)
        │   ├── lists.component.*        # GET/POST/PATCH/DELETE /lists — grid of lists with inline rename + confirm-dialog delete
        │   └── list-detail.component.*  # GET /lists/{id}/items — item grid (thumbnail + code + path), code quick-jump, deep-link ?code=, remove-from-list
        ├── shared/
        │   ├── icon/                    # `IconComponent` (#37) — `<app-icon name="folder" [size]="16" />`, inline-SVG paths for a closed set of names, Lucide-like stroke (currentColor, width 1.7, round caps)
        │   ├── menu/                    # `MenuComponent` (#37) — floating menu at a point or anchored to an element, clamped to the viewport; data-driven items (`MenuItem[]`: id/label/icon/shortcut/danger/disabled/separatorBefore), optional `[menuHeader]` projection, full keyboard nav (arrows/Home/End/Esc/Tab), closes on outside click/right-click
        │   ├── popover/                 # `PopoverComponent` (#37) — anchored panel (below by default, flips above if no room, clamped horizontally), plain content projection, closes on outside click/Esc; same `.pop` visual as the menu container
        │   ├── toast/                   # `ToastService` + `ToastOutletComponent` (#37) — `toastService.show(message, { action?, duration? })`; outlet renders bottom-left stacked, inverse colors (`--text` bg / `--bg` text); one `<app-toast-outlet />` lives in `app.component.html`. Replaced the browser's old local `file-op-message` banner.
        │   ├── infinite-scroll/         # `appInfiniteScroll` directive (#40): IntersectionObserver on a sentinel (300px lookahead, root =
        │   │                           #   nearest overflow-y:auto ancestor), emits `reached`; re-observes whenever `disabled` turns false so
        │   │                           #   a short page keeps loading until the viewport is filled. Callers guard against duplicate pages
        │   ├── load-more-footer/        # `LoadMoreFooterComponent` (#40): progress bar + "N of M" + "Load X more" fallback button, error +
        │   │                           #   Retry (pauses auto-load), "All N items · end of folder|results|list" when done. Doubles as the
        │   │                           #   sentinel. Used by browser, search and list-detail; skeleton tiles via `<app-media-card [skeleton]>`
        │   ├── media-card/              # `MediaCardComponent` (#39) — `<app-media-card kind="video|photo|other" …>`, the one grid tile
        │   │                           #   used by browser, search results and list-detail. Photo: 3:2 cover crop, name without extension
        │   │                           #   below, ext badge on the image only when `[showExt]` (caller decides — browser sets it only when
        │   │                           #   the folder mixes formats). Video: the full 5-frame filmstrip (never cropped to one frame), card
        │   │                           #   in the strip's 1640:180 aspect ratio, name + `EXT · duration` meta below. `other` (non-media
        │   │                           #   untracked files) renders a plain file-icon tile, no preview fetch. Untracked: dimmed, dashed
        │   │                           #   outline, "Untracked" pill. Missing/failed preview (`(error)` on the `<img>`, tracked via an
        │   │                           #   `imgError` signal, reset whenever `previewUrl` changes) → "Generating preview…" skeleton, never
        │   │                           #   a broken-image icon. No tracked-dot. Check circle (top-left) + "⋯" (top-right, anchors the
        │   │                           #   caller's context menu via the `more` output) show on hover/focus, stay visible while `[selecting]`,
        │   │                           #   and the "⋯" alone stays visible under `@media (hover: none)` (touch). Optional `code`/`date`
        │   │                           #   slots (list/search). An `<ng-content>` slot renders the caller's inline rename `<input>` in place
        │   │                           #   of the name/meta caption when `[renaming]` is true — rename logic/state stays in the caller.
        │   ├── file-detail-panel/       # Shared file detail panel (used by browser, search, lists)
        │   ├── image-viewer/            # Zoomable/pannable image viewer used by the detail panel
        │   ├── confirm-dialog/          # Generic confirm/cancel dialog on top of ModalComponent (reused by lists, rename/move, future callers)
        │   ├── quick-jump/              # Header box (#38): one `.jump` field ("Jump to code…", ⌘K/Ctrl+K hint — global, focuses it;
        │   │                           #   Escape collapses it again) with a small truncated list-name button at its left that opens a
        │   │                           #   `MenuComponent` of lists in place of the old native `<select>`; code input still uppercases,
        │   │                           #   Enter jumps to /lists/:id?code=, a 404 turns the border red with an inline "No item with code X"
        │   │                           #   message. Selected list remembered in `localStorage`. Under 760px the field collapses to an
        │   │                           #   icon button that expands it as a fixed-position overlay with a backdrop + close button
        │   ├── list-picker/             # Reusable "add to list" input (text field + keyboard-navigable dropdown + ad hoc create); used by the detail panel and the browser's bulk action bar
        │   ├── folder-picker/           # Directory navigator on top of ModalComponent: breadcrumbs from ROOT_DIR, directories-only listing via POST /files/directory, inline "New folder" (POST /files/mkdir). Reused by two flows via inputs: `title`/`confirmLabel` (default "Move to…"/"Move here"); `sourcePaths` (optional — when given, disables the source itself/its descendants/its current parent as a target; omitted entirely for a plain "pick any folder" flow where nothing is blocked, used by Rediscover's "Rediscover here")
        │   └── rediscover-dialog/       # Starts POST /tracking/rediscover (#25): `path` omitted → folder-picker step ("Rediscover…"/"Rediscover here") then a checkbox confirm step; `path` given (browser context-menu "Rediscover" on a directory) → checkbox confirm step only. Checkbox: "Also track new files" (default off). On start, fires `ApiService.taskRefresh$` and reports "Rediscover started — see tasks."
        ├── modal/                  # Base modal shell (backdrop, teleport-to-body, Esc-to-close)
        ├── maintenance/            # Maintenance page: hosts troubleshooting sections — "Path conflicts" (above) then "Missing files" (below)
        │   ├── path-conflicts/     # Path-conflicts section (#25): GET /tracking/conflicts → one card per md5 (thumbnail if has_preview, file name, keyword/location/list badges), radio list of every path (tracked path first, labelled "currently tracked"; missing paths disabled), per-card "Apply" → POST /tracking/conflicts/resolve; header "Keep all current" / "Use new location for all" → ConfirmDialogComponent → POST /tracking/conflicts/resolve-batch, shows "N resolved · M skipped (reason)"; fires `ApiService.conflictsChanged$` after any resolve so the sidebar badge updates
        │   └── missing-files/      # Missing-files section as its own embeddable component: auto-checks on page open, "Re-check" button, grouped-by-directory cards (thumbnail, keyword/location/list badges), "Rediscover…" button per group opens `app-rediscover-dialog` (#25)
        └── settings/               # Settings page — "Appearance" section (theme segmented control over `ThemeService`)
```

---

## UI Building Blocks (#37)

Reusable pieces on top of the design tokens (#36, `frontend/src/styles.css`), so later pages don't
bring their own button/menu/toast styles.

- **Global utility classes** — `frontend/src/styles/components.css` (imported from `styles.css`,
  documented in a comment block at its top). Buttons: `.btn` + `.btn-primary`/`.btn-ghost`/`.btn-danger`/
  `.btn-on`, `.btn-sm`; `.icon-btn` (+ `.icon-btn-sm`). Segmented control: `.seg` / `.seg-btn.on` /
  `.seg-count` (Settings' theme picker uses these directly — no local CSS anymore). Inputs: plain
  `.input`/`.select`, or `.field` as an icon+input wrapper. Chips: `.chip` (+ `.chip-code`,
  `.chip-remove`) and the dashed `.chip-add`. Misc: `.kbd` (shortcut hint), `.skeleton` (shimmer
  placeholder, static under `prefers-reduced-motion`). Apply directly to native elements — no
  component needed.
- **`shared/icon/`** — `IconComponent`, `<app-icon name="folder" [size]="16" />`. Inline SVG, stroke
  `currentColor`/width 1.7/round caps, matching the prototype. `name` is one of a closed, hand-picked
  set (see `ICONS` in `icon.component.ts`) — never pass user-controlled text as `name`.
- **`shared/menu/`** — `MenuComponent`. Opens at a viewport point (`[position]="{x,y}"`) or anchored
  below-right of an element (`[anchor]`), always clamped inside the viewport (8px margin). Items are
  data (`MenuItem[]`: `id, label, icon?, shortcut?, danger?, disabled?, separatorBefore?`); outputs
  `select(id)` / `closed`. Optional header via `<div menuHeader>…</div>` projection. Keyboard: first
  item focused on open, ArrowUp/Down cycle, Home/End, Enter/Space (native button activation), Esc/Tab
  close. A transparent backdrop behind the menu closes it on outside click or right-click. Like
  `ModalComponent`, it teleports itself to `<body>` on init and removes itself on destroy — the
  caller owns visibility (`@if`) and reacts to `closed`/`select` to tear it down.
- **`shared/popover/`** — `PopoverComponent`. Anchored to an element, opens below by default and
  flips above when there isn't room, clamped horizontally. Plain `<ng-content>` projection (forms,
  the tasks panel, …); closes on outside click or Esc. Same `.pop` visual/teleport convention as
  `MenuComponent`.
- **`shared/toast/`** — `ToastService` (`providedIn: 'root'`) + `ToastOutletComponent`. Call
  `toastService.show('Renamed to "x.jpg"', { action: { label: 'Undo', run: () => … }, duration: 4000 })`
  from anywhere; one `<app-toast-outlet />` in `app.component.html` renders the stack, bottom-left,
  inverse colors (`--text` background / `--bg` text), auto-dismiss (default 4s, `duration: 0` = sticky
  until the dismiss button is clicked). Replaced the browser page's old local `file-op-message`
  signal/banner.
- **Dialogs on tokens** — `modal/`, `shared/confirm-dialog/`, `shared/folder-picker/`,
  `shared/rediscover-dialog/`, `shared/list-picker/` were converted off hardcoded hex colors onto the
  design tokens and the classes above (`--raised` surface, `--r-lg`, `--shadow-2`, `rgba(5,6,8,.6)`
  backdrop, `.btn`/`.btn-primary` footer actions, `.btn-danger` for the destructive confirm path).
  Behaviour/structure unchanged.
- **Not yet redesigned**: lists / health / map / comparison (#45). Shell (#38), cards/grid (#39), infinite scroll (#40), the context menu (#41),
  selection/bulk bar (#42), the detail view (#43) and search (#44) are done (see below).

---

## Database Schema

| Table | PK | Purpose |
|---|---|---|
| `Files` | `md5_hash` | Core catalog: name, extension, media_type, directory, last_indexed_at |
| `FileDetails` | `md5_hash` | Universal metadata: description, recorded_at, last_modified_at, location_id, lat/lon, altitude, json |
| `VideoDetails` | `md5_hash` | Video-specific: codec, resolution, fps, audio info, duration_tc, shot/scene/take/angle/move/shot_type |
| `PhotoDetails` | `md5_hash` | Photo-specific: EXIF (make, model, ISO, aperture, shutter, focal length, color space, lens, 35mm-equiv focal length, scale/crop factor, field of view) |
| `Locations` | `id` (autoincrement) | Reusable named places with hierarchy: country, region, city, name, lat/lon |
| `Keywords` | `id` (autoincrement) | Distinct keyword strings (`keyword` is UNIQUE) |
| `FileKeywords` | `md5_hash + keyword_id` | Join table linking `Files` ↔ `Keywords` (FKs to both) |
| `ClipPreviews` | `md5_hash` | JPEG preview stored as BLOB — 5-frame horizontal strip for videos, single resized thumbnail for photos |
| `Lists` | `id` (autoincrement) | Named user-defined lists of files (`name` is UNIQUE) |
| `ListItems` | `list_id + md5_hash` | Join table linking `Lists` ↔ `Files`, `ON DELETE CASCADE` from `Lists`; each row also carries an `item_code` |
| `FileOperations` | `id` (autoincrement) | Journal of the safe move/rename service (`fileops/`): one row per physical `os.rename` — `kind` (`file_rename`/`dir_move`), `source_path`, `target_path`, `status` (`pending`/`done`/`rolled_back`/`failed`), `error`, `created_at`, `finished_at` |
| `PathConflicts` | `md5_hash + candidate_path` | Open duplicate-path decisions left by a rediscover/scan: `md5_hash` (FK → `Files`, `ON DELETE CASCADE`), `candidate_path`, `source` (`rediscover`/`scan`), `found_at`. The currently-tracked path is never itself a row here. Survives a backend restart (unlike in-memory tasks); pruned automatically once a candidate disappears from disk or becomes the tracked path. |

**Indexes:** `Files.directory` (for fast browser lookups), `Locations.country`, `Locations.city`, `Locations.(country, region, city)`, `Keywords.keyword`, `ListItems.md5_hash`

**List item codes** — each `ListItems` row gets a random 6-character `item_code` drawn from an alphabet without visually-confusable characters (`ABCDEFGHJKMNPQRSTUVWXYZ23456789` — no `0`/`O`, `1`/`I`/`L`; ~700M combinations), unique only *within* its list (`uq__ListItems__list_id_item_code`). Generation/normalization lives in `db/list_codes.py` (`generate_item_code`, `normalize_item_code`); codes are stored upper-case and looked up case-insensitively. The code is stable while a file stays in the list — removing and re-adding it issues a new code. `Database.add_files_to_list` regenerates on collision (checked against the list's existing codes, with a bounded retry loop against a rare `IntegrityError` race).

**Schema is managed by Alembic, not raw SQL.** `db/models.py` is the single source of truth (SQLAlchemy Core `Table` definitions); migrations live in `alembic/versions/`. `app.py` runs `alembic upgrade head` on startup, so the schema self-heals. The old `sql/setup.sql` is legacy and no longer loaded. To change the schema: edit `db/models.py`, then `uv run alembic revision --autogenerate -m "..."` and review the generated migration.

**MD5 as primary key** is intentional: renaming or moving a file won't lose associated metadata. When re-indexing a moved/renamed file, the existing record is relinked by hash only when that's unambiguous (old path gone from disk, found at exactly one new path); otherwise the scan leaves `Files` untouched and records a `PathConflicts` row for a human to resolve (see "Normal scan" below). The DaVinci CSV import (`scan_files_in_metadata`) is the one exception still doing a silent path upsert — out of scope for #26, see its bullet below.

**Keyword normalization** — keywords are deduplicated in `Keywords` and associated to files through `FileKeywords` (replacing the earlier denormalized `md5_hash + keyword` table). Upserts use `ON CONFLICT DO NOTHING` on the keyword string, then link via the join table.

**media_type** is assigned at scan time from configurable extension maps (`MEDIA_TYPE_*` env vars): `video`, `photo`, `360_video`, `360_photo`, or NULL for unrecognised extensions.

**Sidecar/proxy files** (`.xmp`, `.acr`, `.psd`, `.lrv`, `.identifier`) are hidden from the browser via `BROWSER_HIDDEN_EXTENSIONS` but not prevented from being tracked if explicitly requested.

---

## Key Design Decisions

- **Path-based browsing, hash-based tracking** — the frontend browses by filesystem path (fast, intuitive), but records are keyed by MD5 hash so moving/renaming a file doesn't lose its metadata.
- **ROOT_DIR boundary** — `/files/directory` rejects any path outside `ROOT_DIR` (403). The frontend fetches `ROOT_DIR` from `/config` on startup and uses it as the navigation root.
- **Decoupled DB layer** — all database access is isolated in `db/` (`engine.py`, `models.py`, `database.py`). The rest of the app only calls the `Database` class; nothing else imports SQLAlchemy. This makes swapping the backing store a localized change. `engine.py::upsert`/`upsert_ignore` pick the dialect-specific `ON CONFLICT` insert at runtime (`postgresql` or `sqlite`), so the query code stays dialect-agnostic.
- **PostgreSQL as the default DB** — migrated from SQLite. Two roles: an *owner* role for DDL (Alembic) and a least-privilege *app* role for DML (the running app). The engine uses `pool_pre_ping` to survive idle/dropped connections over the network.
- **Alembic for schema evolution** — `db/models.py` is the source of truth; migrations are generated from it and applied automatically on startup (`alembic upgrade head` in `app.py`). No more hand-maintained `setup.sql`.
- **Background tasks** — long-running scans run as background tasks with queryable status, progress reporting, and FAILED state. In-memory only (lost on restart). Jobs run concurrently (Starlette threadpool), but the work *inside* a job (hashing + probing) is fanned out across a single process-wide **shared worker pool** (`tasks/workerpool.py`, `WORKER_POOL_SIZE`). One shared pool means the number of queued jobs is decoupled from total concurrency — no matter how many scans are running, at most `WORKER_POOL_SIZE` files are hashed/probed at once, which also keeps concurrent DB connections under the engine pool's ceiling. Directory scans use it for both phases (`Scanner.scan_files` hashing and the `index_files_in_directory` probe loop); per-file probe failures are isolated so one bad file doesn't abort the scan.
- **Scan populates details automatically** — FFprobe fills `VideoDetails` + `FileDetails.recorded_at` for video files; exiftool fills `PhotoDetails` + `FileDetails.recorded_at` for photos. DaVinci Resolve CSV import can later overwrite with richer editorial metadata via upsert (`ON CONFLICT DO UPDATE`).
- **exiftool for all photo probing** — `photos/exif.py::probe_photo` shells out to `exiftool -json` for every photo format (JPEG, RW2, …), replacing the old split where only RW2 used exiftool and JPEGs used Pillow. One code path, richer/consistent tags (lens, 35mm-equiv focal length, scale/crop factor, FOV), timezone-aware timestamps, and maker-note GPS Pillow couldn't read. Numeric tags use the `#` suffix (`-FNumber#`) for raw values; the "Field Of View" composite is keyed `FOV` in JSON. Pillow/rawpy are retained **only** for thumbnail pixel decoding. exiftool is already a hard dependency (in the Dockerfile).
- **Full EXIF dump on demand** — `GET /files/exif?path=` runs `exiftool -json -G1` and returns every tag as an ordered `[{group, tag, value}]` (read-only, not persisted). The detail panel's "Show all metadata" button opens a modal table grouped by EXIF group. Works for any file type (videos too), not just photos.
- **File-centric tracking, no shot grouping** — RAW+JPEG pairs from the same shot are tracked independently. No "shot" entity for now. Location hierarchy lives in `Locations`; precise GPS per file lives in `FileDetails.latitude/longitude/altitude`.
- **GPS auto-extraction** — EXIF GPS is parsed via exiftool at scan time (signed decimal degrees + altitude) and stored in `FileDetails.latitude/longitude/altitude`. The detail panel map uses named Location coords first, falling back to raw GPS if no location is assigned; altitude shows in the Location column. Lumix RW2 files carry no GPS; phone JPEGs do (incl. altitude).
- **Photo thumbnails reuse ClipPreviews** — `generate_photo_thumbnail()` in `photos/exif.py` produces a 600px-wide JPEG (Pillow for JPEG, EXIF-rotation-corrected; rawpy for RW2). Stored in the same `ClipPreviews` table, served by the same `/files/clip-preview/{md5_hash}` endpoint.
- **DaVinci Resolve CSV** as the primary editorial metadata enrichment path — imports shot/scene/take/angle/move/shot_type directly from Resolve's export.
- **Task poll interval** — configurable via `TASK_POLL_INTERVAL_MS` env var, exposed through `/config` so the frontend picks it up dynamically.
- **Google Maps (runtime-keyed)** — maps use `@angular/google-maps` (Maps JS API + Advanced Markers; geocoding via `google.maps.Geocoder`). The browser API key + Map ID come from `GOOGLE_MAPS_API_KEY`/`GOOGLE_MAPS_MAP_ID`, served to the frontend via `/config` (key stays in `.env`, never in git) and loaded once by `GoogleMapsLoaderService`. Blank key → maps gracefully disabled. Server-side clustering (`/locations/map-points`) is map-library-agnostic and unchanged. Setup: `GOOGLE_SETUP.md`.
- **Safe move/rename with journal recovery + path locks** — `fileops/service.py` backs `PATCH /files/rename` (file or directory), `POST /files/move`/`/move/preview` and `POST /files/mkdir`. Every physical rename is `os.rename` only (never copy+delete; cross-filesystem `EXDEV` fails with a clear error) and is journaled in `FileOperations` as `pending` *before* it's attempted: the DB path update(s) and the `os.rename` run inside one transaction (`Database.run_guarded_rename`), so a failed `rename` rolls the transaction back (journal → `failed`) and a commit that fails *after* a successful rename triggers `os.rename` back (journal → `rolled_back`); the journal row only reaches `done` once both sides agree. `recover_pending_operations()` runs on every startup (`app.py` lifespan, after `alembic upgrade head`) and reconciles any `pending` row left by a crash by checking which side of the rename exists on disk. A file rename/move carries along same-stem sidecars (`BROWSER_HIDDEN_EXTENSIONS`) in the same transaction; a directory rename/move is a single physical rename plus a prefix `UPDATE` on `Files.directory` (escaped `LIKE ... ESCAPE '\'` so renaming `/a/b` never touches `/a/bc`). `fileops/pathlocks.py` is a simple in-process reader/writer lock keyed by resolved path (ancestor/descendant overlap via `Path.is_relative_to`, never string `startswith`): scans/tracking take a blocking `shared()` lock on the subtree they touch, while move/rename take a non-blocking `try_exclusive()` that immediately 409s ("A scan is running in this folder") instead of racing a running scan — there's only one backend process, so no cross-process locking is needed.
- **Rediscover-scan: auto-relink only when unambiguous, conflicts persisted, disk never touched** — `POST /tracking/rediscover` hashes every file under a folder (full MD5, no shortcut via name/size/mtime) and reconciles it against `Files` by hash through `fileops/rediscover.py`. A hash is auto-relinked (`Files.directory`/`file_name`/`file_extension` updated) only when its tracked path is gone from disk *and* the scan found it at exactly one new path — any ambiguity (old path still exists, or the hash now shows up at several paths, or an already-tracked file has another copy lying around) is left alone and recorded as a `PathConflicts` row for a human to resolve later, deduplicated via `ON CONFLICT DO NOTHING` so re-running a rediscover never piles up duplicates. Unknown hashes are only counted unless `track_new` is set, in which case the first (sorted) path is tracked through the normal scan/probe path and any other copies become conflicts against it. Every run also prunes `PathConflicts` rows (scoped to hashes touched this run, or candidates under the scanned folder) whose candidate has since disappeared from disk or now matches the tracked path — a bounded, predictable cleanup rather than a full-table scan. Metadata (`FileDetails`, `Keywords`, `Locations`, `Lists`, `VideoDetails`/`PhotoDetails`) and the filesystem itself are never touched by a rediscover — only `Files`' path columns move.
- **Normal scan reuses the rediscover rules (#26)** — `POST /tracking/scan-directory`/`/scan-file` (`index_files_in_directory`/`index_single_file` in `api/tracking.py`) no longer upsert `Files` directly for a known hash. Both go through a shared `_scan_and_reconcile()` that classifies every hash found against the DB with the same `fileops/rediscover.py::classify()`/`apply()` as `/rediscover` (`source='scan'`, `track_new=True`, `scanned_directory` = the scanned folder for a directory scan or the file's parent for a single-file scan): an unknown hash is inserted + probed (first sorted path wins if found more than once this scan, the rest become conflicts); a hash whose tracked path is among the found paths is re-probed in place to bump `last_indexed_at` (any other copy found in the same scan becomes a conflict, not a probe); a hash whose tracked path is missing on disk and found exactly once is relinked like a rediscover, then probed at the new path; a hash whose tracked path still exists elsewhere, or that's found more than once with the tracked path gone, is left exactly as in `Files` and is **not** probed — only a `PathConflicts` row (`source='scan'`) is recorded. The background task's final progress message is `"Indexed N files · M relinked · K conflicts"` (the literal `"<n> conflicts"` substring is intentional — it's what the frontend's Rediscover-link widget from #25 looks for). `scan_files_in_metadata` (the DaVinci CSV import) is **not** covered by this — it still upserts `Files` by hash directly and can silently move a path; that's out of scope for #26.
- **Single-origin Compose stack** — the frontend's nginx serves the static Angular bundle *and* reverse-proxies `/api` to the backend on the internal network. The browser only talks to one origin, so there's no hardcoded backend host (prod `apiUrl` is the relative `/api`) and CORS is unnecessary. PostgreSQL stays external (NAS); Compose runs only `backend` + `frontend`.

---

## What's Working

- [x] Backend API with FastAPI, PostgreSQL (SQLAlchemy Core), background tasks
- [x] Decoupled DB layer (`db/engine.py` + `db/models.py` + `db/database.py`); dialect-aware upserts
- [x] PostgreSQL migration (from SQLite) with Alembic migrations applied on startup
- [x] Docker Compose full-stack (`backend` + `frontend`/nginx, external Postgres); single-origin nginx reverse-proxy for `/api`, multi-stage frontend image
- [x] Directory scanning with MD5 hashing + media_type assignment → `Files`; reconciled through the rediscover rules (#26) — a known hash only moves when the move is unambiguous, otherwise a `PathConflicts` row is recorded and the path is left alone
- [x] Single file tracking → `Files`; same reconciliation as the directory scan (a copy of an already-tracked file becomes a conflict, not a silent path flip)
- [x] Auto-population of `VideoDetails` from FFprobe on scan
- [x] Auto-population of `PhotoDetails` from exiftool on scan (all photo formats; incl. lens, 35mm-equiv focal length, scale/crop factor, FOV)
- [x] Auto-population of `FileDetails.last_modified_at` + `recorded_at` on scan
- [x] GPS extraction (lat/lon + altitude) from photo EXIF via exiftool → `FileDetails.latitude/longitude/altitude` (auto-shown on map; altitude in Location column)
- [x] `GET /files/exif` — full exiftool tag dump; "Show all metadata" modal in detail panel (grouped table, sticky section headers)
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `VideoDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip for videos, single thumbnail for photos) → `ClipPreviews`
- [x] Missing preview detection + repair endpoint
- [x] Missing-files detection: `GET /trouble-shooting/missing-files` (optional `?path=` subtree, 403 outside ROOT_DIR) — one query (`Database.get_tracked_files_with_attachment_counts`) returns every tracked file with keyword/location/list counts + preview presence, then `os.path.exists` filters to rows missing on disk (no hashing, nothing modified/deleted); frontend "Maintenance" page's "Missing files" section auto-checks on open, re-checkable, grouped by old directory with thumbnail + badges per file, "Rediscover…" per group opens the rediscover dialog (#25)
- [x] `POST /tracking/rediscover` — MD5-based rediscover-scan: auto-relinks uniquely-moved files, persists duplicate-path conflicts to `PathConflicts`, optional `track_new`, with a background-task progress summary (also shown once the task reaches COMPLETED)
- [x] Rediscover + path-conflict resolution from the UI (#25): `GET /tracking/conflicts` groups open `PathConflicts` rows per md5 (`Database.get_tracked_files_with_attachment_counts` extended with an optional `md5_hashes` filter, no N+1) into `{tracked_path, tracked_exists, candidates: [{path, exists, source, found_at}]}` plus the usual keyword/location/list/preview summary; `GET /tracking/conflicts/count` backs the sidebar badge; `POST /tracking/conflicts/resolve` validates `chosen_path` is the tracked path or a known candidate (400), inside ROOT_DIR (403) and exists on disk (409), then repoints `Files` (guarded on the old path) and clears every `PathConflicts` row for that hash in one transaction (`Database.resolve_path_conflict`), disk untouched; `POST /tracking/conflicts/resolve-batch` applies `keep_tracked` (skip if the tracked path is gone) or `use_candidate` (skip if 0 or >1 existing candidates) across a list of hashes, returning `{resolved, skipped: [{md5_hash, reason}]}`. Frontend: `maintenance/path-conflicts/` section above "Missing files" (per-md5 cards with a path radio list + "Apply", header "Keep all current"/"Use new location for all" batch actions behind `ConfirmDialogComponent`); `shared/rediscover-dialog/` starts a rediscover from the missing-files group header (folder picker via `app-folder-picker` + "Also track new files" checkbox) or from the browser's directory context menu ("Rediscover" — same checkbox step, folder already known); the tasks widget shows a "Review conflicts" → `/maintenance` link on a completed Rediscover task whose summary reports a non-zero conflict count, and fires `ApiService.conflictsChanged$` on that transition so the sidebar badge and an open conflicts section reload
- [x] `GET /config` endpoint (root_dir, task_poll_interval_ms, google_maps_api_key, google_maps_map_id)
- [x] `POST /files/directory` with sorting, pagination, ROOT_DIR hardening, hidden extension filtering; optional `kind` (`video`/`photo`/`untracked`, #46) filters the listing to files of that media-type class (same classification as the frontend's `VIDEO_TYPES`/`PHOTO_TYPES`), excludes directories, and pagination then refers to the filtered set — omitted = unchanged default behaviour; response carries `counts: {directories, video, photo, untracked}` for the *whole* directory (independent of pagination and of any `kind` filter, so filter-segment labels stay correct while paginated) and each directory entry (`PathChild`) carries `file_count` (direct, non-hidden files in that subfolder, not recursive, cheap `os.scandir`, `null` if unreadable); tracked video entries also carry `duration_tc` (`VideoDetails.duration_tc`, `HH:MM:SS:FF`, joined in the same query that already loads tracked-file status — no per-file queries), `null` for photos/untracked/directories, formatted by the frontend (`formatDurationTc` in `models.ts`) as `mm:ss` or `h:mm:ss` once ≥ 1h for the media card's `EXT · duration` caption (#39)
- [x] `GET /files/details` — filesystem info + DB tracking status + VideoDetails/PhotoDetails per file
- [x] `PATCH /files/rename` — rename a file *or directory* on disk + update `Files` record(s), via the safe move/rename service (journaled, path-locked)
- [x] `POST /files/move` + `/files/move/preview`, `POST /files/mkdir` — bulk/single file move, directory move, dry-run counts (file/tracked/sidecars), new-folder creation; see "Safe move/rename with journal recovery + path locks" above
- [x] Background task FAILED status with error message
- [x] Background task progress reporting (step messages while running)
- [x] Angular shell: 72px left rail (bottom tab bar on mobile) + topbar with a page-driven breadcrumb slot, lazy routing (#38)
- [x] Browser page: directory navigation with breadcrumbs, load-more pagination
- [x] File detail panel: two-column layout (metadata left, location+map right), tracking dot next to filename
- [x] Inline filename editing in detail view (pen icon on hover → input → Enter to save, Escape to cancel)
- [x] Right-click context menu: "Scan directory" / "Track file" triggers tracking
- [x] Tasks widget in header: live badge, polling, progress, FAILED display, per-task dismiss
- [x] Keywords/tags: add + remove from detail panel, autocomplete from all existing keywords
- [x] Location management: `GET/POST /locations`, `PATCH /files/location` — create + assign from detail panel
- [x] Keyword API: `GET /keywords` (all), `POST /keywords` (add), `DELETE /keywords` (remove) — backed by normalized `Keywords` + `FileKeywords`
- [x] Faceted search API: `POST /files/search` (filter by media_type, keywords, country, date range, camera make/model, video codec, lists + item code; paginated; `item_code` per result when exactly one list is filtered) + `GET /files/search-facets` (autocomplete for facet values)
- [x] Map data API: `GET /locations/map-points` — server-side clustering by zoom level (grid rounding), video/photo counts per cluster
- [x] AI shot classification: `POST /ai/classify-shot` — ML shot-type prediction for a tracked video (`shot_classifier/`)
- [x] Interactive map in "New location" modal: Google Maps, click-to-pin, draggable Advanced Marker, geocoding via `google.maps.Geocoder` with progressive retry (drops region/name on failure, max 3 attempts)
- [x] Read-only location map in file detail panel (Google Maps, zoom/pan enabled) — shows named location coords or raw GPS fallback
- [x] Search by list: "Lists" filter chips + code field (one list selected), code badges on result cards, a unique code hit opens the detail panel; deep link `/search?list=<id>&code=<code>`
- [x] Header quick jump: list select (remembered in localStorage) + code input → opens the item in the list view; unknown codes flag the input red
- [x] Bulk edit mode in grid: "Select" button → checkbox selection → assign location or add keyword to all selected tracked files in parallel, or add the selection to a list (existing or ad hoc via the reusable `app-list-picker`) with a single `POST /lists/{id}/items` call, untracked files skipped and called out; sticky action bar with transient result message ("12 added to 'X' · 3 already in list · 2 untracked skipped"); ESC to cancel
- [x] Photo thumbnails in browser grid and detail panel (600px JPEG, EXIF-rotation-corrected, `object-fit: contain` in detail view to avoid cropping)
- [x] Tracked status badge on files in browser grid listing
- [x] Lists backend: `Lists`/`ListItems` schema + `api/lists.py` (CRUD, bulk add/remove, paginated items, code lookup); random per-list item codes (`db/list_codes.py`); `GET /files/details` reports list memberships (`FileInfo.lists`)
- [x] Lists frontend: sidebar "Lists" nav entry; overview page (create, inline rename, delete via confirm-dialog with item count); list detail page (item grid with thumbnail/big monospace code/truncated path, code quick-jump with 404 handling, deep-link `?code=` on load, click-to-open shared file detail panel, per-item remove via confirm dialog, 500-page-size load-more); reusable `ConfirmDialogComponent` on top of `ModalComponent`; header "Export PDF" button (hidden when the list is empty) downloads `GET /lists/{id}/export.pdf`
- [x] List PDF export: `GET /lists/{id}/export.pdf?cols=4&rows=7` renders an A4 sheet of cut-out cards via `exports/list_cards_pdf.py` (reportlab, base-14 fonts only) — big bold letter-spaced item code, small grey relative path (2-line wrap, else truncated from the start with a leading `…` so the file name stays visible), faint (#DDDDDD, 0.3pt) shared grid lines with no doubling even on a partial last page, small footer (list name · date · page n/m); ASCII-safe + RFC 5987 `Content-Disposition` filename
- [x] Lists in the shared file detail panel (browser/search/lists): "Lists" section below keywords showing `Name · CODE` pills (tracked files only), pill name links to `/lists/:id?code=`, × removes via `ConfirmDialogComponent` ("...code will be released"); add-to-list input with a custom keyboard-navigable dropdown (existing lists filtered by text, trailing "+ Create list" entry when no exact match) that creates the list ad hoc and adds the file in one step; `listsChanged` output lets `list-detail` reload its grid/count and close the panel when the open item's own list membership was removed; `list-detail` now subscribes to paramMap/queryParamMap (reading both from `route.snapshot` to stay atomic across a single navigation) instead of a one-time snapshot read, so a detail-panel pill can jump between two list-detail routes without a stale-list 404 or a missed reload
- [x] Rename and move from the browser UI, on top of #21's `fileops/` service: context menu gained "Rename" and "Move to…" for both files and directories (kept "Scan directory"/"Track file"), emitting a typed `{ kind, entry }` action instead of overloading the old single-purpose emitter. Rename is an inline edit directly on the grid tile (dir chip or file card — mirrors the detail panel's pen-icon/input/Enter-save/Escape-cancel UX); a directory rename always confirms first via `ConfirmDialogComponent` with counts from a `POST /files/move/preview` dry run (`paths=[dir], target_directory=<parent>`), a file rename only confirms when sidecars (same-stem `BROWSER_HIDDEN_EXTENSIONS`) would move along too. "Move to…" opens the new `app-folder-picker` (breadcrumbs from ROOT_DIR, directories-only listing, inline "New folder" via `POST /files/mkdir` that navigates straight into the new folder, source/descendant/current-parent disabled as targets with a tooltip), then confirms via `ConfirmDialogComponent` with counts + sidecars + the target path relative to ROOT_DIR before calling `POST /files/move`. Bulk mode's action bar gained a "Move to…" button for the current (files-only — directories were already not selectable in bulk mode, so the backend's single-directory-path constraint is moot) selection. Both flows report results via a transient banner ("2 moved", or "0 moved · 1 failed: Target already exists: …" with the backend's `detail` inlined, dismissible, non-sticky on full success), then reload the current directory listing; if the viewed directory (or an ancestor of it) was itself renamed/moved the view follows it to its new path, and if the open detail panel was showing a moved/renamed file it re-fetches it at the new path (or closes if it's gone).
- [x] UI redesign #36 — design tokens + base styles (dark/light): `frontend/src/styles.css` is now the single source of truth for the visual system — CSS custom properties on `:root` for color (`--bg --surface --raised --hover --line --line-strong --text --muted --faint --stage --accent --accent-ink --accent-soft --ok --danger --skeleton --skeleton-hi`), shadows (`--shadow-1/2`), type scale (`--fs-xs/sm/md/lg/xl`), radii (`--r-sm/md/lg`), spacing (`--space-1…8`, 4px grid) and motion (`--dur-fast/base`); values taken verbatim from the design prototype. Dark is the default (no `data-theme` attr, or `data-theme="dark"`); `:root[data-theme="light"]` carries the light palette. **No hex colors in component CSS** — every color comes from a token; only the shell (`app.component.css`) and `tasks-widget` were touched to use tokens so far, the rest of the component tree keeps its old hardcoded styling until its own redesign ticket lands. `services/theme.service.ts` (signal-based, `providedIn: 'root'`, injected once from `AppComponent`) owns the theme choice (`system`/`dark`/`light`, `localStorage` key `fa-theme`, default `dark`, all storage access wrapped in try/catch) and resolves `system` via `matchMedia('(prefers-color-scheme: light)')` (kept live via a change listener), writing `data-theme`/`color-scheme` onto `<html>`. A matching inline script in `index.html` applies the stored choice before Angular bootstraps, so there's no flash of the wrong theme. Settings gained an "Appearance" section with a small local segmented control (System/Dark/Light) — not the shared `app-ui` building blocks, those come with #37. Fonts: Geist + Geist Mono bundled locally via `@fontsource/geist`/`@fontsource/geist-mono` (weights 400/500/600, mono 400/500, imported from `styles.css`) so the app works offline on the NAS; `--font`/`--mono` fall back to system stacks.
- [x] UI redesign #37 — building blocks on top of #36's tokens (see "UI Building Blocks" section above for the full rundown): global utility classes (`frontend/src/styles/components.css`) for buttons/icon-buttons/segmented-control/inputs/chips/kbd/skeleton; `shared/icon/` (`IconComponent`, inline-SVG Lucide-like icon set); `shared/menu/` (`MenuComponent` — point- or anchor-positioned, viewport-clamped, keyboard-navigable floating menu); `shared/popover/` (`PopoverComponent` — anchored, flips above when it doesn't fit below); `shared/toast/` (`ToastService` + `ToastOutletComponent`, bottom-left stacked, replaces the browser page's old local `file-op-message` banner). `modal/`, `shared/confirm-dialog/`, `shared/folder-picker/`, `shared/rediscover-dialog/`, `shared/list-picker/` converted to tokens + the new classes (no hex colors left, behaviour unchanged); Settings' theme picker now uses the global `.seg`/`.seg-btn` classes instead of a local copy. The browser's own context menu (`browser/context-menu/`) and card/grid styling are untouched here — they're #41/#39.
- [x] UI redesign #38 — app shell on top of #36/#37: `app.component.*` is now a 72px `.rail` (brand mark, `app-icon`-based nav items for Browse/Search/Lists/Map/Health/Settings, active item = `--hover` bg + 3px left accent bar, compact two-line mono version footer) over a `.main` column with a 52px `.topbar` (breadcrumb slot + `app-quick-jump` + `app-tasks-widget`); burger/`sidebarOpen`/collapsible-label logic is gone. New `services/header.service.ts` (`HeaderService`) lets a page publish `{label, action?}` breadcrumbs (last = bold/current) or a plain title; the shell falls back to the route's `title` (`app.routes.ts`) when a page sets nothing, clearing on every `NavigationStart` so the previous page's trail never flashes. The browser page publishes its path breadcrumbs through it via an `effect()` (its own `nav` now only holds the local Scan/Select buttons — #39 turns those into a proper toolbar); Lists/Maintenance/Search dropped their redundant H2/filter-panel heading now that the topbar is the single title. `shared/quick-jump/` gained a `MenuComponent`-backed list picker (replacing the native `<select>`) inside a prototype-style `.jump` field, plus a global Cmd/Ctrl+K `HostListener` and a mobile icon-button-to-overlay collapse. `tasks-widget/` gained the SVG progress ring (average fraction across running tasks' `progress` text, indeterminate spin with no numeric match), a failed-task red dot, and moved its popover onto `PopoverComponent`/full tokens; "Clear finished" calls `DELETE /tasks/completed` for COMPLETED tasks plus a per-id delete for FAILED ones (`ApiService.clearCompletedTasks()`), leaving running/queued tasks untouched — a more honest label than the old "Clear all", which deleted everything including in-flight scans. Below 760px the rail becomes a bottom tab bar (`.tabbar`, `env(safe-area-inset-bottom)`) with Health/Settings tucked behind a "More" `MenuComponent`, the quick-jump field collapses to an icon that expands as a fixed overlay, and the tasks popover goes full-width with 8px margins.
- [x] UI redesign #44 — search page: filter rail with toggle chips (type, lists), token inputs and a stacked
  date range; a result header with the count and every active filter as a removable chip (`activeFilters`
  computed) plus "Clear all"; empty states with "All videos"/"All photos" quick starts; full-bleed
  (`page-flush`), videos in two columns. On phones the filter rail becomes a sheet behind a "Filters (n)" button.
- [x] UI redesign #43 — detail view rebuilt in `shared/file-detail-panel/`: top bar (Back to <folder|results|list>,
  position, Load full resolution), dark stage (photo in `image-viewer`, video = all 5 clip-preview frames at
  native size with timecodes at 1/6…5/6 of the duration), fixed 360px inspector on the right (header with rename
  + status, Keywords, Lists, Location with a filterable picker popover + "New location…", Capture, Shot
  classification, File + All metadata) and a filmstrip of neighbours. Hosts pass `navItems`/`navIndex`/
  `backLabel` and handle `navigate`/`jump`; ←/→ step (panel's own HostListener, viewer gets `showNav=false`).
  Browser, search and list-detail all navigate. Untracked files get a "Track file" button. Phone: stacked.
- [x] UI redesign #42 — selection without a mode switch: the card's check circle, Cmd/Ctrl-click or
  Shift-click (range over `orderedFiles`, anchored at the last clicked tile) starts it; a plain click opens
  as before unless selection is active. The old top `bulk-bar` became a floating bar at the bottom (Keyword,
  Location, Add to list, Move, Compare, All/None, ✕; Esc exits) whose inputs open as popovers above the
  buttons. Results go to toasts (`… · N untracked skipped`). Selected tiles get an accent outline.
- [x] UI redesign #41 — context menu on `shared/menu/` with header, icons, groups and shortcuts; viewport-
  clamped, keyboard-navigable, reachable via "⋯" on touch. New: Add keyword…/Add to list… popovers anchored at
  the tile, Copy path (clipboard API with a textarea fallback for plain-http NAS access). `PopoverComponent`
  got `align="start|end"`; `list-picker` only swallows Esc while its suggestions are open.
- [x] UI redesign #40 — infinite scroll in browser, search and list-detail: `shared/infinite-scroll/` +
  `shared/load-more-footer/` replace the old "Load more" link and "Showing all N items" bar; skeleton cards
  show where the next page lands. On mobile the header breadcrumbs shorten to "‹ parent / current".
- [x] UI redesign #39 — shared media card + browser toolbar on top of #36/#37/#38: `shared/media-card/`
  (`MediaCardComponent`, see "UI Building Blocks" above) replaces the three hand-rolled card markups in
  browser/search/list-detail. Browser gained a `.toolbar` row below the topbar — `.seg`/`.seg-btn` filter
  (All/Videos/Stills/Untracked, counts from the directory response's `counts`, a zero-count segment hidden
  except All; picking one reloads via `DirectoryQuery.kind`), a thumbnail-size slider (`--thumb` custom
  property on the grid container, 140–320px range, `localStorage` `fa-thumb` behind try/catch, default
  200), "Scan folder" (`.btn-ghost` + scan icon, swapped from the old upload icon) and "Select" (`.btn`,
  `.btn-on` + "Done" label while bulk mode is active). Folders render as `.folder` tiles (icon, name,
  `file_count` from #46, omitted when null) instead of the old chip row, hidden while a filter segment
  other than All is active (matching the prototype). Section headings ("Videos N"/"Stills N"/"Untracked
  N") read `counts`, not however many rows are loaded on the current page. The photo ext badge
  (`[showExt]`) only shows when the loaded photo entries actually mix extensions. Grid CSS:
  `repeat(auto-fill, minmax(var(--thumb), 1fr))` for stills, `repeat(auto-fill, minmax(min(100%,
  calc(var(--thumb) * 3.2)), 1fr))` for videos (2 columns desktop, 1 on mobile) — `browser.component.css`
  is hex-free. The inline rename input (dir chip and file card) is unchanged behaviourally, now projected
  into the media card's content slot. The card's "⋯" button emits `more` with itself as the anchor
  element; the browser computes the context menu's position from its `getBoundingClientRect()` and opens
  the existing `app-context-menu` there (#41 will redo that menu itself). Search results and list-detail
  swap their card markup for `MediaCardComponent` too (code/date slots) with a minimal token pass over
  their surrounding containers so they read correctly in dark — their own redesigns are #44/#45.

---

## What's Next (Priority Order)

### 1. Tag/Keyword Browsing (frontend)
Backend keyword + search APIs exist (`api/keywords.py`, `api/search.py`). What's missing is the browser UI to list tags and filter files by tag/facet.

### 2. Description / Notes Field
`FileDetails.description` exists in the DB but is not yet exposed in the UI or API.

### 3. `recorded_at` Field
Populated on scan, not yet shown in the detail panel.

### 4. Settings Page
Configure extension maps, trigger manual scans, view task history.
