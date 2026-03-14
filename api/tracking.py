import logging
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, BackgroundTasks

from api.dtos import FileQuery
from davinci.davinciresolve import Metadata, DerivedMetadataColumns
from db.database import Database
from ffmpeg.ffmpeg import FFmpegInput, FFmpeg, FFprobe
from scanner.scanner import Scanner
from tasks.taskmanager import TaskManager, TaskRequest

import pandas as pd

TrackingApi = APIRouter(prefix='/tracking')


@TrackingApi.post('/scan-directory')
async def scan_directory(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is not a directory')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Scan directory',
            description=f'Scanning directory "{query.path}".',
            method=lambda report: index_files_in_directory(query, report)
        ),
        background_tasks
    )

    return task.id


@TrackingApi.post('/scan-file')
async def scan_file(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')
    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Track file',
            description=f'Tracking file "{query.path}".',
            method=lambda report: index_single_file(query, report)
        ),
        background_tasks
    )

    return task.id


@TrackingApi.post('/import-metadata')
async def import_metadata(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')

    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Import metadata',
            description=f'Importing metadata from "{query.path}".',
            method=lambda report: scan_files_in_metadata(query, report)
        ),
        background_tasks
    )

    return task.id


def index_files_in_directory(query: FileQuery, report: Callable[[str], None]):
    directory = Path(query.path)
    report('Scanning files…')
    scan_results = Scanner().scan_directory(directory)
    total = len(scan_results)
    report(f'Indexing 0 / {total} files')
    Database().insert_scan_results(scan_results)

    if query.generate_clip_preview:
        for i, sc in enumerate(scan_results, 1):
            report(f'Generating preview {i} / {total}')
            file_path = (sc.directory + '/' + sc.file_name)
            ffmpeg_input = FFprobe().probe_file(
                md5_hash=sc.md5_hash,
                file_path=file_path
            )
            if ffmpeg_input is not None:
                create_clip_preview(ffmpeg_input)
            else:
                logging.error(f'FFprobe failed for {file_path}')


def index_single_file(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    report('Hashing file…')
    scan_results = Scanner().scan_files([path])
    Database().insert_scan_results(scan_results)

    if query.generate_clip_preview and scan_results:
        report('Generating preview…')
        sc = scan_results[0]
        file_path = sc.directory + '/' + sc.file_name
        ffmpeg_input = FFprobe().probe_file(md5_hash=sc.md5_hash, file_path=file_path)
        if ffmpeg_input is not None:
            create_clip_preview(ffmpeg_input)
        else:
            logging.error(f'FFprobe failed for {file_path}')


def scan_files_in_metadata(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    report('Parsing metadata…')
    metadata = Metadata(path)
    records = metadata.get_details()
    keywords = metadata.get_keywords()
    total = len(records)
    report(f'Hashing {total} files…')
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

    report('Writing to database…')
    Database().connect().insert_scan_results(scan_results)
    Database().connect().insert_file_details(details_merged)
    Database().connect().insert_keywords(keywords_merged)

    if query.generate_clip_preview:
        for i, row in enumerate(details_merged.itertuples(index=True, name='Row'), 1):
            report(f'Generating preview {i} / {total}')
            input = FFmpegInput.from_time_code(md5_hash=row.md5_hash, file_path=row.file_path, duration_tc=row.duration_tc)
            create_clip_preview(input)


def create_clip_preview(input: FFmpegInput):
    result = FFmpeg(input.md5_hash).generate_clip_preview(input)
    Database().connect().insert_clip_preview(result, identifier=input.md5_hash)
