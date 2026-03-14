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
```

**Test footage** lives in `footage/` (gitignored):
```
footage/japan_2024/
├── video/
│   ├── nara/           (4 MOV files)
│   └── atami/
│       ├── beach/      (4 MOV files)
│       ├── castle/     (3 MOV files)
│       ├── shopping_street/ (3 MOV files)
│       └── station/    (3 MOV files)
├── photo/
├── 360/
└── phone/
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
- `ROOT_DIR=/mnt/user/footage`  (or wherever footage lives)
- `SCANNING_FILE_EXTENSIONS=.mov,.mp4,.jpg,.jpeg,.png`
- `TASK_POLL_INTERVAL_MS=5000`  (default, optional)

---

## Architecture

```
footage-archive/
├── app.py                  # FastAPI entry point, lifespan, CORS, DB init
├── api/
│   ├── base.py             # GET / (redirect to /docs), GET /version
│   ├── config.py           # GET /config  ← root_dir, task_poll_interval_ms
│   ├── files.py            # POST /files/directory, GET /files/details, POST /files/checksum
│   ├── tracking.py         # POST /tracking/scan-directory, /scan-file, /import-metadata
│   ├── tasks.py            # GET/DELETE /tasks, DELETE /tasks/{id}
│   ├── troubleshoot.py     # GET/POST /trouble-shooting/missing-preview
│   └── tags.py             # empty placeholder
├── db/database.py          # SQLite via sqlite3 + pandas, upsert pattern
├── scanner/scanner.py      # recursive dir walk + MD5 hashing
├── ffmpeg/ffmpeg.py        # FFprobe duration probe + keyframe JPEG strip
├── davinci/davinciresolve.py  # DaVinci Resolve CSV metadata parser
├── tasks/taskmanager.py    # in-memory singleton background task queue
├── env/environment.py      # env var reader with fallbacks
├── sql/
│   ├── setup.sql           # schema (4 tables)
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
| `Files` | `md5_hash` | Core catalog: name, extension, directory, last_indexed_at |
| `FileDetails` | `md5_hash` | Rich metadata: codec, resolution, fps, shot/scene/take/angle, description, recorded_at |
| `Keywords` | `md5_hash + keyword` | Tag associations |
| `ClipPreviews` | `md5_hash` | Horizontal JPEG keyframe strip stored as BLOB |

**MD5 as primary key** is intentional: renaming or moving a file won't lose associated metadata. When re-indexing a file that has been moved/renamed, the existing record is recovered by hash.

---

## Key Design Decisions

- **Path-based browsing, hash-based tracking** — the frontend browses by filesystem path (fast, intuitive), but records are keyed by MD5 hash so moving/renaming a file doesn't lose its metadata.
- **ROOT_DIR boundary** — `/files/directory` rejects any path outside `ROOT_DIR` (403). The frontend fetches `ROOT_DIR` from `/config` on startup and uses it as the navigation root.
- **Background tasks** — long-running scans run as background tasks with queryable status, progress reporting, and FAILED state. In-memory only (lost on restart).
- **DaVinci Resolve CSV** as the primary metadata enrichment path — imports shot/scene/take/angle/move/shot_type directly from Resolve's export.
- **Task poll interval** — configurable via `TASK_POLL_INTERVAL_MS` env var, exposed through `/config` so the frontend picks it up dynamically.

---

## What's Working

- [x] Backend API with FastAPI, SQLite, background tasks
- [x] Directory scanning with MD5 hashing → `Files` table
- [x] Single file tracking → `Files` table
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip) → `ClipPreviews`
- [x] Missing preview detection + repair endpoint
- [x] `GET /config` endpoint (root_dir, task_poll_interval_ms)
- [x] `POST /files/directory` with sorting, pagination, and ROOT_DIR hardening
- [x] `GET /files/details` — filesystem info + DB tracking status per file
- [x] Background task FAILED status with error message
- [x] Background task progress reporting (step messages while running)
- [x] Angular shell: header with page title, collapsible dark sidebar, lazy routing
- [x] Browser page: directory navigation with breadcrumbs, load-more pagination
- [x] File detail panel: opens on click, shows name/size/modified/tracking status
- [x] Right-click context menu: "Scan directory" / "Track file" triggers tracking
- [x] Tasks widget in header: live badge, polling, progress, FAILED display, per-task dismiss

---

## What's Next (Priority Order)

### 1. Tracked/Untracked Status in Browser
The browser currently shows all files without indicating tracking state. Add per-entry indicators:
- ✅ **Tracked** — file is in the `Files` DB table (matched by path)
- ⬜ **Untracked** — file exists on disk but has no DB record
- ❌ **Missing** — file is in DB but no longer on disk

Requires a backend endpoint that cross-references a directory listing with the DB.

### 2. File Detail Panel — Rich Metadata
The detail panel currently shows only filesystem info. Extend it with:
- Rich metadata from `FileDetails` (codec, resolution, fps, shot/scene/take)
- Keywords/tags from `Keywords`
- Clip preview thumbnail from `ClipPreviews`

Requires new read endpoints for `FileDetails` and `ClipPreviews`.

### 3. Tag/Keyword Browsing
`api/tags.py` is an empty placeholder. Implement tag listing, filtering files by tag.

### 4. Settings Page
Configure `SCANNING_FILE_EXTENSIONS`, trigger manual scans, view task history.

### 5. PostgreSQL Migration
SQLite is fine for now. If/when needed: replace the raw `sqlite3 + pandas` layer with SQLAlchemy, which supports both SQLite and PostgreSQL.
