import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from fastapi import APIRouter, HTTPException, BackgroundTasks

from api.dtos import FileQuery
from davinci.davinciresolve import Metadata, DerivedMetadataColumns
from db.database import Database
from ffmpeg.ffmpeg import FFmpegInput, FFmpeg, FFprobe
from photos.exif import probe_photo, generate_photo_thumbnail
from scanner.scanner import Scanner, ScanResult
from tasks.taskmanager import TaskManager, TaskRequest

TrackingApi = APIRouter(prefix='/tracking')

VIDEO_TYPES = {'video', '360_video'}
PHOTO_TYPES = {'photo', '360_photo'}


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
    report(f'Found {total} files, indexing…')
    db = Database()
    db.insert_scan_results(scan_results)

    for i, sc in enumerate(scan_results, 1):
        report(f'Probing {i} / {total}: {sc.file_name}')
        _probe_and_save(sc, db, generate_clip_preview=query.generate_clip_preview)


def index_single_file(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    report('Hashing file…')
    scan_results = Scanner().scan_files([path])
    if not scan_results:
        return
    sc = scan_results[0]
    db = Database()
    db.insert_scan_results(scan_results)
    report('Probing file…')
    _probe_and_save(sc, db, generate_clip_preview=query.generate_clip_preview)


def _probe_and_save(sc: ScanResult, db: Database, generate_clip_preview: bool):
    file_path = sc.directory + '/' + sc.file_name
    last_modified_at = datetime.fromtimestamp(Path(file_path).stat().st_mtime).isoformat()

    if sc.media_type in VIDEO_TYPES:
        probe = FFprobe().probe_file(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            logging.warning(f'FFprobe failed for {file_path}')
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
            return

        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at)
        db.insert_video_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            create_clip_preview(probe)

    elif sc.media_type in PHOTO_TYPES:
        probe = probe_photo(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
        else:
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at,
                               latitude=probe.latitude, longitude=probe.longitude,
                               altitude=probe.altitude)
            db.insert_photo_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            thumbnail = generate_photo_thumbnail(sc.md5_hash, file_path)
            if thumbnail:
                db.insert_raw_preview(sc.md5_hash, thumbnail, identifier=sc.md5_hash)

    else:
        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)


def _save_file_details(db: Database, md5_hash: str, last_modified_at: str, recorded_at: str | None,
                       latitude: float | None = None, longitude: float | None = None,
                       altitude: float | None = None):
    df = pd.DataFrame([{
        'md5_hash': md5_hash,
        'last_modified_at': last_modified_at,
        'recorded_at': recorded_at,
        'latitude': latitude,
        'longitude': longitude,
        'altitude': altitude,
    }])
    db.insert_file_details(df)


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
    db = Database()
    db.insert_scan_results(scan_results)
    db.insert_file_details(details_merged)
    db.insert_video_details(details_merged)
    db.insert_keywords(keywords_merged)

    if query.generate_clip_preview:
        for i, row in enumerate(details_merged.itertuples(index=True, name='Row'), 1):
            report(f'Generating preview {i} / {total}')
            ffmpeg_input = FFmpegInput.from_time_code(
                md5_hash=row.md5_hash,
                file_path=row.file_path,
                duration_tc=row.duration_tc
            )
            create_clip_preview(ffmpeg_input)


def create_clip_preview(input: FFmpegInput):
    result = FFmpeg(input.md5_hash).generate_clip_preview(input)
    if result is not None:
        Database().insert_clip_preview(result, identifier=input.md5_hash)
