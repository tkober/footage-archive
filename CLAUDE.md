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
| Preview generation | FFmpeg + FFprobe + Pillow |
| Containerisation | Docker (linux/amd64 for Unraid) |

---

## Running Locally

**Prerequisites:**

FFmpeg (provides `ffmpeg` + `ffprobe`, required for video probing and clip previews):
```bash
brew install ffmpeg
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

## Deploying to Unraid

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
- `TASK_POLL_INTERVAL_MS=5000` (default, optional)

---

## Architecture

```
footage-archive/
├── app.py                  # FastAPI entry point, lifespan, CORS, DB init
├── api/
│   ├── base.py             # GET / (redirect to /docs), GET /version
│   ├── config.py           # GET /config  ← root_dir, task_poll_interval_ms
│   ├── files.py            # POST /files/directory, GET /files/details, PATCH /files/rename, GET /files/clip-preview/{md5_hash}, PATCH /files/location, POST /files/checksum
│   ├── search.py           # GET /files/search-facets (facet autocomplete), POST /files/search (filtered, paginated search)
│   ├── keywords.py         # GET /keywords (all), POST /keywords (add to file), DELETE /keywords (remove from file)
│   ├── locations.py        # GET /locations, POST /locations (create), GET /locations/map-points (clustered map markers)
│   ├── tracking.py         # POST /tracking/scan-directory, /scan-file, /import-metadata
│   ├── ai.py               # POST /ai/classify-shot — ML shot-type classification for a tracked video
│   ├── tasks.py            # GET /tasks, GET /tasks/{id}, DELETE /tasks/completed, DELETE /tasks/{id}
│   ├── troubleshoot.py     # GET /trouble-shooting/missing-preview, POST /trouble-shooting/missing-preview/fix
│   └── dtos.py             # Pydantic request/response models (search query/results, etc.)
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
├── photos/exif.py          # Pillow EXIF extraction → PhotoProbeResult + GPS DMS parsing + thumbnail generation
├── davinci/davinciresolve.py  # DaVinci Resolve CSV metadata parser
├── shot_classifier/classifier.py  # ML shot-type classifier (backs POST /ai/classify-shot)
├── tasks/taskmanager.py    # in-memory singleton background task queue
├── env/environment.py      # env var reader with fallbacks; builds DB URLs from DB_URL + DB_USER/DB_OWNER_USER
├── sql/                    # LEGACY raw-SQL files (setup.sql etc.) — superseded by Alembic + db/models.py, no longer loaded
└── frontend/               # Angular 21 app
    └── src/app/
        ├── app.component.*         # shell: header + tasks widget + collapsible sidebar
        ├── app.routes.ts           # lazy-loaded routes
        ├── app.config.ts           # provideRouter + provideHttpClient
        ├── models.ts               # TypeScript interfaces
        ├── services/api.service.ts # HTTP calls + taskRefresh$ subject
        ├── tasks-widget/           # Header task indicator with polling + progress
        ├── browser/                # Browser page: directory navigator + file detail panel
        │   └── context-menu/       # Right-click context menu (scan/track actions)
        └── settings/               # Settings page (empty placeholder)
```

---

## Database Schema

| Table | PK | Purpose |
|---|---|---|
| `Files` | `md5_hash` | Core catalog: name, extension, media_type, directory, last_indexed_at |
| `FileDetails` | `md5_hash` | Universal metadata: description, recorded_at, last_modified_at, location_id, lat/lon, json |
| `VideoDetails` | `md5_hash` | Video-specific: codec, resolution, fps, audio info, duration_tc, shot/scene/take/angle/move/shot_type |
| `PhotoDetails` | `md5_hash` | Photo-specific: EXIF (make, model, ISO, aperture, shutter, focal length, color space) |
| `Locations` | `id` (autoincrement) | Reusable named places with hierarchy: country, region, city, name, lat/lon |
| `Keywords` | `id` (autoincrement) | Distinct keyword strings (`keyword` is UNIQUE) |
| `FileKeywords` | `md5_hash + keyword_id` | Join table linking `Files` ↔ `Keywords` (FKs to both) |
| `ClipPreviews` | `md5_hash` | JPEG preview stored as BLOB — 5-frame horizontal strip for videos, single resized thumbnail for photos |

**Indexes:** `Files.directory` (for fast browser lookups), `Locations.country`, `Locations.city`, `Locations.(country, region, city)`, `Keywords.keyword`

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
- **Background tasks** — long-running scans run as background tasks with queryable status, progress reporting, and FAILED state. In-memory only (lost on restart).
- **Scan populates details automatically** — FFprobe fills `VideoDetails` + `FileDetails.recorded_at` for video files; Pillow EXIF fills `PhotoDetails` + `FileDetails.recorded_at` for photos. DaVinci Resolve CSV import can later overwrite with richer editorial metadata via upsert (`ON CONFLICT DO UPDATE`).
- **File-centric tracking, no shot grouping** — RAW+JPEG pairs from the same shot are tracked independently. No "shot" entity for now. Location hierarchy lives in `Locations`; precise GPS per file lives in `FileDetails.latitude/longitude`.
- **GPS auto-extraction** — EXIF GPS (DMS format) is parsed from JPEGs at scan time and stored in `FileDetails.latitude/longitude`. The detail panel map uses named Location coords first, falling back to raw GPS if no location is assigned. RAW formats (RW2, DNG) don't carry extractable GPS via Pillow.
- **Photo thumbnails reuse ClipPreviews** — `generate_photo_thumbnail()` in `photos/exif.py` produces a 600px-wide JPEG via Pillow (EXIF-rotation-corrected). Stored in the same `ClipPreviews` table, served by the same `/files/clip-preview/{md5_hash}` endpoint. RAW files silently skip (Pillow can't decode them).
- **DaVinci Resolve CSV** as the primary editorial metadata enrichment path — imports shot/scene/take/angle/move/shot_type directly from Resolve's export.
- **Task poll interval** — configurable via `TASK_POLL_INTERVAL_MS` env var, exposed through `/config` so the frontend picks it up dynamically.

---

## What's Working

- [x] Backend API with FastAPI, PostgreSQL (SQLAlchemy Core), background tasks
- [x] Decoupled DB layer (`db/engine.py` + `db/models.py` + `db/database.py`); dialect-aware upserts
- [x] PostgreSQL migration (from SQLite) with Alembic migrations applied on startup
- [x] Directory scanning with MD5 hashing + media_type assignment → `Files`
- [x] Single file tracking → `Files`
- [x] Auto-population of `VideoDetails` from FFprobe on scan
- [x] Auto-population of `PhotoDetails` from Pillow EXIF on scan
- [x] Auto-population of `FileDetails.last_modified_at` + `recorded_at` on scan
- [x] GPS coordinate extraction from JPEG EXIF → `FileDetails.latitude/longitude` (auto-shown on map in detail panel)
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `VideoDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip for videos, single thumbnail for photos) → `ClipPreviews`
- [x] Missing preview detection + repair endpoint
- [x] `GET /config` endpoint (root_dir, task_poll_interval_ms)
- [x] `POST /files/directory` with sorting, pagination, ROOT_DIR hardening, hidden extension filtering
- [x] `GET /files/details` — filesystem info + DB tracking status + VideoDetails/PhotoDetails per file
- [x] `PATCH /files/rename` — rename file on disk + update Files record
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
- [x] Faceted search API: `POST /files/search` (filter by media_type, keywords, country, date range, camera make/model, video codec; paginated) + `GET /files/search-facets` (autocomplete for facet values)
- [x] Map data API: `GET /locations/map-points` — server-side clustering by zoom level (grid rounding), video/photo counts per cluster
- [x] AI shot classification: `POST /ai/classify-shot` — ML shot-type prediction for a tracked video (`shot_classifier/`)
- [x] Interactive map in "New location" modal: Leaflet + OSM tiles, click-to-pin, draggable marker, geocoding via Nominatim with progressive retry (drops region/name on failure, max 3 attempts)
- [x] Read-only location map in file detail panel (zoom/pan enabled) — shows named location coords or raw GPS fallback
- [x] Bulk edit mode in grid: "Select" button → checkbox selection → assign location or add keyword to all selected tracked files in parallel; sticky action bar; ESC to cancel
- [x] Photo thumbnails in browser grid and detail panel (600px JPEG, EXIF-rotation-corrected, `object-fit: contain` in detail view to avoid cropping)
- [x] Tracked status badge on files in browser grid listing

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
