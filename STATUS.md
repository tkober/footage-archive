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

---

## Architecture

```
footage-archive/
├── app.py                  # FastAPI entry point, lifespan, CORS, DB init
├── api/
│   ├── base.py             # GET / (redirect to /docs), GET /version
│   ├── config.py           # GET /config  ← returns root_dir + future config
│   ├── files.py            # POST /files/directory, POST /files/checksum
│   ├── scanning.py         # POST /scanning/directory, POST /scanning/metadata
│   ├── tasks.py            # GET/DELETE /tasks
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
        ├── app.component.*         # shell: header + collapsible sidebar
        ├── app.routes.ts           # lazy-loaded routes
        ├── app.config.ts           # provideRouter + provideHttpClient
        ├── models.ts               # TypeScript interfaces (Config, PathChild, etc.)
        ├── services/api.service.ts # HTTP calls to backend
        ├── browser/                # Browser page (directory navigator)
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
- **Background tasks** — long-running scans run as background tasks with queryable status. Currently in-memory only (lost on restart, no failure state).
- **DaVinci Resolve CSV** as the primary metadata enrichment path — imports shot/scene/take/angle/move/shot_type directly from Resolve's export.

---

## What's Working

- [x] Backend API with FastAPI, SQLite, background tasks
- [x] Directory scanning with MD5 hashing → `Files` table
- [x] DaVinci Resolve CSV metadata ingestion → `FileDetails` + `Keywords`
- [x] Clip preview generation (5-frame JPEG strip) → `ClipPreviews`
- [x] Missing preview detection + repair endpoint
- [x] `GET /config` endpoint (root_dir, extensible)
- [x] `POST /files/directory` with sorting, pagination, and ROOT_DIR hardening
- [x] Angular shell: header with page title, collapsible dark sidebar, lazy routing
- [x] Browser page: directory navigation with breadcrumbs, folder/file icons

---

## What's Next (Priority Order)

### 1. Tracked/Untracked Status in Browser
The browser currently shows all files. The next step is to indicate per-file:
- ✅ **Tracked** — file is in the `Files` DB table (matched by path)
- ⬜ **Untracked** — file exists on disk but has no DB record
- ❌ **Missing** — file is in DB but no longer on disk

Requires a new backend endpoint that cross-references a directory listing with the DB.

### 2. Trigger Scanning from the Browser
A button/context action to scan a directory and index its contents, using the existing `POST /scanning/directory` endpoint. Show task progress.

### 3. File Detail Panel
Click a file to see:
- Rich metadata from `FileDetails` (codec, resolution, fps, shot/scene/take)
- Keywords/tags from `Keywords`
- Clip preview thumbnail from `ClipPreviews`

Requires new read endpoints for `FileDetails` and `ClipPreviews` (currently data is written but never read back).

### 4. Tag/Keyword Browsing
`api/tags.py` is an empty placeholder. Implement tag listing, filtering files by tag.

### 5. Settings Page
Configure `SCANNING_FILE_EXTENSIONS`, trigger manual scans, view task history.

### 6. Task Error Handling
Currently if a background task throws, it stays stuck in `RUNNING` forever with the error silently swallowed. Needs a `FAILED` status and error message.

### 7. PostgreSQL Migration
SQLite is fine for now. If/when needed: replace the raw `sqlite3 + pandas` layer with SQLAlchemy, which supports both SQLite and PostgreSQL.
