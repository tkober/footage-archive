from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks

from api.dtos import FileQuery
from davinci.davinciresolve import Metadata, DerivedMetadataColumns
from db.database import Database
from ffmpeg.ffmpeg import FFmpegInput, FFmpeg, FFprobe
from scanner.scanner import Scanner
from tasks.taskmanager import TaskManager, TaskRequest

import pandas as pd

ScanningApi = APIRouter(prefix='/scanning')


@ScanningApi.post('/directory')
async def scan(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is not a directory')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Scan directory',
            description=f'Scanning directory "{query.path}".',
            method=lambda: index_files_in_directory(query)
        ),
        background_tasks
    )

    return task.id


def index_files_in_directory(query: FileQuery):
    directory = Path(query.path)
    scan_results = Scanner().scan_directory(directory)
    Database().insert_scan_results(scan_results)

    if query.generate_clip_preview:
        for sc in scan_results:
            create_clip_preview(FFprobe().probe_file(
                md5_hash=sc.md5_hash,
                file_path=(sc.directory + '/' + sc.file_name)
            ))


@ScanningApi.post('/metadata')
async def scan_metadata_file(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')

    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Scanning files from Metadata',
            description=f'Scanning files from metadata in "{query.path}".',
            method=lambda: scan_files_in_metadata(query)
        ),
        background_tasks
    )

    return task.id


def scan_files_in_metadata(query: FileQuery):
    path = Path(query.path)
    metadata = Metadata(path)
    records = metadata.get_details()
    keywords = metadata.get_keywords()
    scan_results = Scanner().scan_files(records[DerivedMetadataColumns.FILE_PATH.value])

    df = pd.DataFrame([r.model_dump() for r in scan_results])
    df[DerivedMetadataColumns.FILE_PATH.value] = df['directory'] + '/' + df['file_name']

    details_merged = pd.merge(
        left=df[['md5_hash', DerivedMetadataColumns.FILE_PATH.value]],
        right=records,
        on=DerivedMetadataColumns.FILE_PATH.value
    )

    keywords_merged = pd.merge(
        left=df[['md5_hash', DerivedMetadataColumns.FILE_PATH.value]],
        right=keywords,
        on=DerivedMetadataColumns.FILE_PATH.value
    )

    Database().connect().insert_scan_results(scan_results)
    Database().connect().insert_file_details(details_merged)
    Database().connect().insert_keywords(keywords_merged)

    if query.generate_clip_preview:
        for row in details_merged.itertuples(index=True, name='Row'):
            input = FFmpegInput.from_time_code(md5_hash=row.md5_hash, file_path=row.file_path, duration_tc=row.duration_tc)
            create_clip_preview(input)


def create_clip_preview(input: FFmpegInput):
    result = FFmpeg(input.md5_hash).generate_clip_preview(input)
    Database().connect().insert_clip_preview(result)
