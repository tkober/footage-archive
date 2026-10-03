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
│   ├── tracking.py         # POST /tracking/scan-directory, /scan-file, /import-metadata
│   ├── ai.py               # POST /ai/classify-shot — ML shot-type classification for a tracked video
│   ├── tasks.py            # GET /tasks, GET /tasks/{id}, DELETE /tasks/completed, DELETE /tasks/{id}
│   ├── troubleshoot.py     # GET /trouble-shooting/missing-preview, POST /trouble-shooting/missing-preview/fix
│   └── dtos.py             # Pydantic request/response models (search query/results, etc.)
├── exports/
│   └── list_cards_pdf.py   # Pure PDF renderer (no DB access): A4 grid of cut-out cards for a list — big bold item code, small grey truncated/wrapped relative path, faint shared grid lines, page footer
├── fileops/                # Safe move/rename/mkdir service backing api/files.py's PATCH /rename, POST /move(/preview), POST /mkdir
│   ├── pathlocks.py        # Process-wide in-memory reader/writer path lock registry: shared(path) (scans/tracking block while an overlapping move is in flight) + try_exclusive(paths) (move/rename raises PathLockedError immediately, never waits, on any overlapping shared/exclusive lock); overlap is Path.is_relative_to on resolved paths, never string startswith
│   └── service.py          # Validation (ROOT_DIR, exists/absent, no self-move, name sanity) + os.rename-only physical moves/renames with sidecar handling + DB path updates, each physical rename journaled in FileOperations before it is attempted; recover_pending_operations() reconciles any 'pending' row left by a crash
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
        ├── app.component.*         # shell: header + tasks widget + collapsible sidebar
        ├── app.routes.ts           # lazy-loaded routes
        ├── app.config.ts           # provideRouter + provideHttpClient
        ├── models.ts               # TypeScript interfaces
        ├── services/api.service.ts # HTTP calls + taskRefresh$ subject
        ├── tasks-widget/           # Header task indicator with polling + progress
        ├── browser/                # Browser page: directory navigator + file detail panel
        │   └── context-menu/       # Right-click context menu (scan/track actions)
        ├── search/                 # Faceted search page: filter panel + results grid + sliding detail panel
        ├── map/                    # Map page: Google Maps clustering, flyouts, "open in search"
        ├── lists/                  # Lists feature: overview (create/rename/delete) + list detail (item grid, code jump, remove)
        │   ├── lists.component.*        # GET/POST/PATCH/DELETE /lists — grid of lists with inline rename + confirm-dialog delete
        │   └── list-detail.component.*  # GET /lists/{id}/items — item grid (thumbnail + code + path), code quick-jump, deep-link ?code=, remove-from-list
        ├── shared/
        │   ├── file-detail-panel/       # Shared file detail panel (used by browser, search, lists)
        │   ├── image-viewer/            # Zoomable/pannable image viewer used by the detail panel
        │   ├── confirm-dialog/          # Generic confirm/cancel dialog on top of ModalComponent (reused by lists, future callers)
        │   ├── quick-jump/              # Header box: pick a list + type an item code → /lists/:id?code= (gachapon use case)
        │   └── list-picker/             # Reusable "add to list" input (text field + keyboard-navigable dropdown + ad hoc create); used by the detail panel and the browser's bulk action bar
        ├── modal/                  # Base modal shell (backdrop, teleport-to-body, Esc-to-close)
        └── settings/               # Settings page (empty placeholder)
```

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

**Indexes:** `Files.directory` (for fast browser lookups), `Locations.country`, `Locations.city`, `Locations.(country, region, city)`, `Keywords.keyword`, `ListItems.md5_hash`

**List item codes** — each `ListItems` row gets a random 6-character `item_code` drawn from an alphabet without visually-confusable characters (`ABCDEFGHJKMNPQRSTUVWXYZ23456789` — no `0`/`O`, `1`/`I`/`L`; ~700M combinations), unique only *within* its list (`uq__ListItems__list_id_item_code`). Generation/normalization lives in `db/list_codes.py` (`generate_item_code`, `normalize_item_code`); codes are stored upper-case and looked up case-insensitively. The code is stable while a file stays in the list — removing and re-adding it issues a new code. `Database.add_files_to_list` regenerates on collision (checked against the list's existing codes, with a bounded retry loop against a rare `IntegrityError` race).

**Schema is managed by Alembic, not raw SQL.** `db/models.py` is the single source of truth (SQLAlchemy Core `Table` definitions); migrations live in `alembic/versions/`. `app.py` runs `alembic upgrade head` on startup, so the schema self-heals. The old `sql/setup.sql` is legacy and no longer loaded. To change the schema: edit `db/models.py`, then `uv run alembic revision --autogenerate -m "..."` and review the generated migration.

**MD5 as primary key** is intentional: renaming or moving a file won't lose associated metadata. When re-indexing a moved/renamed file the existing record is recovered by hash.

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
- **Single-origin Compose stack** — the frontend's nginx serves the static Angular bundle *and* reverse-proxies `/api` to the backend on the internal network. The browser only talks to one origin, so there's no hardcoded backend host (prod `apiUrl` is the relative `/api`) and CORS is unnecessary. PostgreSQL stays external (NAS); Compose runs only `backend` + `frontend`.

---

## What's Working

- [x] Backend API with FastAPI, PostgreSQL (SQLAlchemy Core), background tasks
- [x] Decoupled DB layer (`db/engine.py` + `db/models.py` + `db/database.py`); dialect-aware upserts
- [x] PostgreSQL migration (from SQLite) with Alembic migrations applied on startup
- [x] Docker Compose full-stack (`backend` + `frontend`/nginx, external Postgres); single-origin nginx reverse-proxy for `/api`, multi-stage frontend image
- [x] Directory scanning with MD5 hashing + media_type assignment → `Files`
- [x] Single file tracking → `Files`
- [x] Auto-population of `VideoDetails` from FFprobe on scan
- [x] Auto-population of `PhotoDetails` from exiftool on scan (all photo formats; incl. lens, 35mm-equiv focal length, scale/crop factor, FOV)
- [x] Auto-population of `FileDetails.last_modified_at` + `recorded_at` on scan
- [x] GPS extraction (lat/lon + altitude) from photo EXIF via exiftool → `FileDetails.latitude/longitude/altitude` (auto-shown on map; altitude in Location column)
- [x] `GET /files/exif` — full exiftool tag dump; "Show all metadata" modal in detail panel (grouped table, sticky section headers)
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `VideoDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip for videos, single thumbnail for photos) → `ClipPreviews`
- [x] Missing preview detection + repair endpoint
- [x] `GET /config` endpoint (root_dir, task_poll_interval_ms, google_maps_api_key, google_maps_map_id)
- [x] `POST /files/directory` with sorting, pagination, ROOT_DIR hardening, hidden extension filtering
- [x] `GET /files/details` — filesystem info + DB tracking status + VideoDetails/PhotoDetails per file
- [x] `PATCH /files/rename` — rename a file *or directory* on disk + update `Files` record(s), via the safe move/rename service (journaled, path-locked)
- [x] `POST /files/move` + `/files/move/preview`, `POST /files/mkdir` — bulk/single file move, directory move, dry-run counts (file/tracked/sidecars), new-folder creation; see "Safe move/rename with journal recovery + path locks" above
- [x] Background task FAILED status with error message
- [x] Background task progress reporting (step messages while running)
- [x] Angular shell: header with page title, collapsible dark sidebar, lazy routing
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
