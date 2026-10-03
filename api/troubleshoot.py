import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException

from api.dtos import MissingFile
from api.tracking import create_clip_preview
from db.database import Database
from env.environment import Environment
from ffmpeg.ffmpeg import FFprobe
from fileops.pathlocks import shared
from tasks.taskmanager import TaskManager, TaskRequest

TroubleShootingApi = APIRouter(prefix='/trouble-shooting')

_env = Environment()


@TroubleShootingApi.get('/missing-preview')
async def get_missing_previews():
    return Database().get_files_without_clip_preview().to_dict(orient="records")


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
    files = Database().get_files_without_clip_preview()
    total = len(files)
    for i, row in enumerate(files.itertuples(index=True, name='Row'), 1):
        if not Path(row.file_path).exists():
            continue
        with shared(row.file_path):
            report(f'Generating preview {i} / {total}')
            ffmpeg_input = FFprobe().probe_file(row.md5_hash, row.file_path)
            create_clip_preview(ffmpeg_input)
