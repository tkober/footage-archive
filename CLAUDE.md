# Footage Archive — Project Status

Personal media management tool for cataloguing travel footage (photos & videos).
Runs on an Unraid NAS server, edited over a 5Gbit network.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn |
| Database | SQLite (PostgreSQL migration considered for future) |
| Package manager | uv (pyproject.toml + uv.lock) |
| Frontend | Angular 21.2, TypeScript 5.9 |
| Preview generation | FFmpeg + FFprobe + Pillow |
| Containerisation | Docker (linux/amd64 for Unraid) |

---

## Running Locally

**Backend:**
```bash
# from project root
uv sync
uv run python app.py
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
DB_PATH=./footage_archive.sqlite
ROOT_DIR=./footage
MEDIA_TYPE_VIDEO=.mov,.mp4
MEDIA_TYPE_PHOTO=.jpg,.jpeg,.rw2
MEDIA_TYPE_360_VIDEO=.insv
MEDIA_TYPE_360_PHOTO=.insp,.dng
BROWSER_HIDDEN_EXTENSIONS=.xmp,.acr,.psd,.lrv,.identifier
```

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
- `DB_PATH=/backup/footage_archive.sqlite`
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
│   ├── files.py            # POST /files/directory, GET /files/details, PATCH /files/rename, POST /files/checksum
│   ├── tracking.py         # POST /tracking/scan-directory, /scan-file, /import-metadata
│   ├── tasks.py            # GET/DELETE /tasks, DELETE /tasks/{id}
│   ├── troubleshoot.py     # GET/POST /trouble-shooting/missing-preview
│   └── tags.py             # empty placeholder
├── db/database.py          # SQLite via sqlite3 + pandas, upsert pattern
├── scanner/scanner.py      # recursive dir walk + MD5 hashing, media_type assignment
├── ffmpeg/ffmpeg.py        # FFprobe (full stream info → VideoProbeResult) + clip preview
├── photos/exif.py          # Pillow EXIF extraction → PhotoProbeResult
├── davinci/davinciresolve.py  # DaVinci Resolve CSV metadata parser
├── tasks/taskmanager.py    # in-memory singleton background task queue
├── env/environment.py      # env var reader with fallbacks
├── sql/
│   ├── setup.sql           # schema (7 tables)
│   └── missing_clip_previews.sql
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
| `Keywords` | `md5_hash + keyword` | Tag associations |
| `ClipPreviews` | `md5_hash` | Horizontal JPEG keyframe strip stored as BLOB |

**Indexes:** `Files.directory` (for fast browser lookups), `Locations.country`, `Locations.city`, `Locations.(country, region, city)`

**MD5 as primary key** is intentional: renaming or moving a file won't lose associated metadata. When re-indexing a moved/renamed file the existing record is recovered by hash.

**media_type** is assigned at scan time from configurable extension maps (`MEDIA_TYPE_*` env vars): `video`, `photo`, `360_video`, `360_photo`, or NULL for unrecognised extensions.

**Sidecar/proxy files** (`.xmp`, `.acr`, `.psd`, `.lrv`, `.identifier`) are hidden from the browser via `BROWSER_HIDDEN_EXTENSIONS` but not prevented from being tracked if explicitly requested.

---

## Key Design Decisions

- **Path-based browsing, hash-based tracking** — the frontend browses by filesystem path (fast, intuitive), but records are keyed by MD5 hash so moving/renaming a file doesn't lose its metadata.
- **ROOT_DIR boundary** — `/files/directory` rejects any path outside `ROOT_DIR` (403). The frontend fetches `ROOT_DIR` from `/config` on startup and uses it as the navigation root.
- **Background tasks** — long-running scans run as background tasks with queryable status, progress reporting, and FAILED state. In-memory only (lost on restart).
- **Scan populates details automatically** — FFprobe fills `VideoDetails` + `FileDetails.recorded_at` for video files; Pillow EXIF fills `PhotoDetails` + `FileDetails.recorded_at` for photos. DaVinci Resolve CSV import can later overwrite with richer editorial metadata via `INSERT OR REPLACE`.
- **File-centric tracking, no shot grouping** — RAW+JPEG pairs from the same shot are tracked independently. No "shot" entity for now. Location hierarchy lives in `Locations`; precise GPS per file lives in `FileDetails`.
- **DaVinci Resolve CSV** as the primary editorial metadata enrichment path — imports shot/scene/take/angle/move/shot_type directly from Resolve's export.
- **Task poll interval** — configurable via `TASK_POLL_INTERVAL_MS` env var, exposed through `/config` so the frontend picks it up dynamically.

---

## What's Working

- [x] Backend API with FastAPI, SQLite, background tasks
- [x] Directory scanning with MD5 hashing + media_type assignment → `Files`
- [x] Single file tracking → `Files`
- [x] Auto-population of `VideoDetails` from FFprobe on scan
- [x] Auto-population of `PhotoDetails` from Pillow EXIF on scan
- [x] Auto-population of `FileDetails.last_modified_at` + `recorded_at` on scan
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `VideoDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip) → `ClipPreviews`
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
- [x] Interactive map in "New location" modal: Leaflet + OSM tiles, click-to-pin, draggable marker, geocoding via Nominatim with progressive retry (drops region/name on failure, max 3 attempts)
- [x] Read-only location map in file detail panel (zoom/pan enabled)
- [x] Bulk edit mode in grid: "Select" button → checkbox selection → assign location or add keyword to all selected tracked files in parallel; sticky action bar; ESC to cancel

---

## What's Next (Priority Order)

### 1. Tag/Keyword Browsing
`api/tags.py` is an empty placeholder. Implement tag listing and filtering files by tag from the browser.

### 2. Description / Notes Field
`FileDetails.description` exists in the DB but is not yet exposed in the UI or API.

### 3. `recorded_at` Field
Populated on scan, not yet shown in the detail panel.

### 4. Settings Page
Configure extension maps, trigger manual scans, view task history.

### 5. PostgreSQL Migration
SQLite is fine for now. When needed: replace `sqlite3 + pandas` with SQLAlchemy.
