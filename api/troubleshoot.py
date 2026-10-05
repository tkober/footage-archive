import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException

from api.dtos import MissingFile, RemoveMissingFilesRequest, RemoveMissingFilesResponse
from api.tracking import PHOTO_TYPES, VIDEO_TYPES, generate_preview
from db.database import Database
from env.environment import Environment
from ffmpeg.ffmpeg import FFprobe
from fileops.pathlocks import shared
from tasks.taskmanager import TaskManager, TaskRequest

TroubleShootingApi = APIRouter(prefix='/trouble-shooting')

_env = Environment()

# Media types that can ever get a clip preview (incl. 360 variants) — a
# non-media file (media_type NULL) is never in scope for either the listing
# or the repair (#65).
_PREVIEWABLE_MEDIA_TYPES = VIDEO_TYPES | PHOTO_TYPES


@TroubleShootingApi.get('/missing-preview')
async def get_missing_previews():
    return Database().get_files_without_clip_preview(_PREVIEWABLE_MEDIA_TYPES).to_dict(orient="records")


@TroubleShootingApi.get('/missing-files')
def get_missing_files(path: Optional[str] = None) -> list[MissingFile]:
    """Tracked Files rows whose path no longer exists on disk — typically
    after a manual move/rename outside the app. Synchronous (no stat'ing
    beyond os.path.exists, no hashing), so it runs in the threadpool rather
    than as a background task. ``path``, if given, restricts the check to
    that subtree and must be inside ROOT_DIR."""
    directory = None
    if path is not None:
        root = Path(_env.get_root_dir())
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
        directory = str(resolved)

    rows = Database().get_tracked_files_with_attachment_counts(directory)
    missing = [row for row in rows if not os.path.exists(os.path.join(row['directory'], row['file_name']))]
    missing.sort(key=lambda r: (r['directory'], r['file_name']))
    return [MissingFile(**row) for row in missing]


@TroubleShootingApi.post('/missing-files/remove')
def remove_missing_files(request: RemoveMissingFilesRequest) -> RemoveMissingFilesResponse:
    """Permanently drop the given tracked files and all their metadata
    (keywords, location, list memberships, preview). Only hashes that are
    still tracked AND still missing on disk are removed — anything that
    reappeared since the listing, or is no longer tracked, is skipped, so
    this can never untrack a file that actually exists."""
    db = Database()
    requested = list(dict.fromkeys(request.md5_hashes))
    tracked = db.get_tracked_paths_for_hashes(requested)
    to_remove = [
        md5_hash for md5_hash, row in tracked.items()
        if not os.path.exists(os.path.join(row['directory'], row['file_name']))
    ]
    removed = db.delete_files(to_remove)
    return RemoveMissingFilesResponse(removed=removed, skipped=len(requested) - removed)


@TroubleShootingApi.post('/missing-preview/fix')
async def fix_missing_previews(background_tasks: BackgroundTasks):
    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Fixing missing previews',
            description='Trying to generate previews for files without.',
            method=lambda report: generate_missing_clip_previews(report)
        ),
        background_tasks
    )


def generate_missing_clip_previews(report):
    """Repair pass for GET /trouble-shooting/missing-preview (#65). Only
    video/photo files are ever listed (see _PREVIEWABLE_MEDIA_TYPES), so
    every row here goes through `generate_preview` for its actual
    media_type instead of assuming video. Each file is isolated in its own
    try/except — a bad probe or a corrupt file is logged + counted as
    failed, never aborting the rest of the batch. A file gone from disk
    since the listing was built is skipped and counted as missing, not
    failed."""
    files = Database().get_files_without_clip_preview(VIDEO_TYPES | PHOTO_TYPES)
    total = len(files)

    generated = 0
    failed = 0
    missing = 0

    for i, row in enumerate(files.itertuples(index=True, name='Row'), 1):
        report(f'Generating preview {i} / {total}')

        if not Path(row.file_path).exists():
            missing += 1
            continue

        try:
            with shared(row.file_path):
                probe = None
                if row.media_type in VIDEO_TYPES:
                    probe = FFprobe().probe_file(row.md5_hash, row.file_path)
                    if probe is None:
                        raise RuntimeError(f'FFprobe failed for {row.file_path}')
                if generate_preview(row.md5_hash, row.file_path, row.media_type, probe=probe):
                    generated += 1
        except Exception:
            logging.exception(f'Failed to generate preview for {row.file_path}')
            failed += 1

    parts = [f"Generated {generated} preview{'' if generated == 1 else 's'}"]
    if failed:
        parts.append(f"{failed} failed")
    if missing:
        parts.append(f"{missing} missing on disk")
    report(' · '.join(parts))
