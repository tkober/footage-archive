from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from api.dtos import DirectoryQuery, DirectoryResponse, FileInfo, FileQuery, PathChild, PathType, FileDescriptor, SortField, SortOrder, VideoDetails, PhotoDetails, RenameRequest, AssignLocationRequest, LocationDto, ExifTag
from db.database import Database
from env.environment import Environment
from photos.exif import dump_all_exif
from scanner.scanner import Scanner

FilesApi = APIRouter(prefix='/files')

_env = Environment()


@FilesApi.post('/directory')
async def query_directory(query: DirectoryQuery) -> DirectoryResponse:
    root = Path(_env.get_root_dir())
    path = Path(query.path).resolve()

    if not path.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not path.exists():
        raise HTTPException(status_code=404, detail='Path does not exist')
    if not path.is_dir():
        raise HTTPException(status_code=400, detail='Path is not a directory')

    hidden = set(_env.get_browser_hidden_extensions())
    tracked = Database().get_tracked_files_in_directory(str(path))

    entries = [
        PathChild(
            name=e.name,
            path=str(e),
            type=PathType.DIRECTORY if e.is_dir() else PathType.FILE,
            file_extension=e.suffix.lower() or None,
            tracked=e.name in tracked if e.is_file() else None,
            md5_hash=tracked[e.name]['md5_hash'] if e.is_file() and e.name in tracked else None,
            media_type=tracked[e.name]['media_type'] if e.is_file() and e.name in tracked else None,
        )
        for e in path.iterdir()
        if not e.name.startswith('._')
        and (e.is_dir() or e.suffix.lower() not in hidden)
    ]

    reverse = query.sort_order == SortOrder.DESC

    if query.sort_by == SortField.NAME:
        entries.sort(key=lambda e: e.name.lower(), reverse=reverse)
    elif query.sort_by == SortField.TYPE:
        entries.sort(key=lambda e: e.type.value, reverse=reverse)

    if query.dirs_first:
        dirs = [e for e in entries if e.type == PathType.DIRECTORY]
        files = [e for e in entries if e.type == PathType.FILE]
        entries = dirs + files

    total = len(entries)
    start = (query.page - 1) * query.page_size
    items = entries[start:start + query.page_size]

    return DirectoryResponse(total=total, page=query.page, page_size=query.page_size, items=items)


def _build_file_info(p: Path, db: Database) -> FileInfo:
    stat = p.stat()
    db_record = db.get_file_by_path(str(p))
    video_details = None
    photo_details = None
    keywords = []
    location = None
    if db_record:
        md5 = db_record['md5_hash']
        media_type = db_record['media_type']
        keywords = db.get_keywords(md5)
        loc_row = db.get_location_for_file(md5)
        if loc_row:
            location = LocationDto(**loc_row)
        if media_type in ('video', '360_video'):
            raw = db.get_video_details(md5)
            if raw:
                video_details = VideoDetails(**raw)
        elif media_type in ('photo', '360_photo'):
            raw = db.get_photo_details(md5)
            if raw:
                photo_details = PhotoDetails(**raw)
    gps = db.get_file_gps(db_record['md5_hash']) if db_record else None
    return FileInfo(
        name=p.name,
        path=str(p),
        file_extension=p.suffix.lower() or None,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
        tracked=db_record is not None,
        md5_hash=db_record['md5_hash'] if db_record else None,
        media_type=db_record['media_type'] if db_record else None,
        last_indexed_at=db_record['last_indexed_at'] if db_record else None,
        video_details=video_details,
        photo_details=photo_details,
        keywords=keywords,
        location=location,
        latitude=gps[0] if gps else None,
        longitude=gps[1] if gps else None,
        altitude=gps[2] if gps else None,
    )


@FilesApi.get('/details')
async def get_file_details(path: str) -> FileInfo:
    root = Path(_env.get_root_dir())
    p = Path(path).resolve()

    if not p.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not p.exists():
        raise HTTPException(status_code=404, detail='File does not exist')
    if p.is_dir():
        raise HTTPException(status_code=400, detail='Path is a directory')

    return _build_file_info(p, Database())


@FilesApi.get('/exif')
async def get_file_exif(path: str) -> list[ExifTag]:
    root = Path(_env.get_root_dir())
    p = Path(path).resolve()

    if not p.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not p.exists():
        raise HTTPException(status_code=404, detail='File does not exist')
    if p.is_dir():
        raise HTTPException(status_code=400, detail='Path is a directory')

    return [ExifTag(**t) for t in dump_all_exif(str(p))]


@FilesApi.patch('/rename')
async def rename_file(request: RenameRequest) -> FileInfo:
    root = Path(_env.get_root_dir())
    p = Path(request.path).resolve()

    if not p.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not p.exists():
        raise HTTPException(status_code=404, detail='File not found')
    if p.is_dir():
        raise HTTPException(status_code=400, detail='Path is a directory')

    new_name = request.new_name.strip()
    if not new_name or '/' in new_name or '\\' in new_name:
        raise HTTPException(status_code=400, detail='Invalid filename')

    new_path = p.parent / new_name
    if new_path.exists():
        raise HTTPException(status_code=409, detail='A file with that name already exists')

    db = Database()
    db_record = db.get_file_by_path(str(p))

    p.rename(new_path)

    if db_record:
        db.rename_file(db_record['md5_hash'], new_name)

    return _build_file_info(new_path, db)


@FilesApi.get('/clip-preview/{md5_hash}')
async def get_clip_preview(md5_hash: str):
    data = Database().get_clip_preview(md5_hash)
    if data is None:
        raise HTTPException(status_code=404, detail='No clip preview found')
    return Response(content=data, media_type='image/jpeg')


@FilesApi.patch('/location')
async def assign_location(request: AssignLocationRequest) -> FileInfo:
    db = Database()
    db_record = db.get_file_by_hash(request.md5_hash)
    if db_record is None:
        raise HTTPException(status_code=404, detail='File not found')
    db.assign_location(request.md5_hash, request.location_id)
    p = Path(db_record['directory']) / db_record['file_name']
    return _build_file_info(p, db)


@FilesApi.post('/checksum')
async def get_checksum(query: FileQuery) -> FileDescriptor:
    path = Path(query.path)

    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')

    return FileDescriptor(
        name=path.name,
        path=str(path),
        type=PathType.FILE,
        file_extension=path.suffix,
        md5_hash=Scanner().md5_hash(str(path))
    )
