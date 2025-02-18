from pathlib import Path

from fastapi import APIRouter, BackgroundTasks

from api.scanning import create_clip_preview
from db.database import Database
from ffmpeg.ffmpeg import FFprobe
from tasks.taskmanager import TaskManager, TaskRequest

TroubleShootingApi = APIRouter(prefix='/trouble-shooting')


@TroubleShootingApi.get('/missing-preview')
async def get_missing_previews():
    return Database().get_files_without_clip_preview().to_dict(orient="records")


@TroubleShootingApi.post('/missing-preview/fix')
async def fix_missing_previews(background_tasks: BackgroundTasks):
    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Fixing missing previews',
            description='Trying to generate previews for files without.',
            method=lambda: generate_missing_clip_previews()
        ),
        background_tasks
    )


def generate_missing_clip_previews():
    files = Database().get_files_without_clip_preview()
    for row in files.itertuples(index=True, name='Row'):
        if not Path(row.file_path).exists():
            continue

        ffmpeg_input = FFprobe().probe_file(row.md5_hash, row.file_path)
        create_clip_preview(ffmpeg_input)
