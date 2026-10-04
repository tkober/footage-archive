import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from api.dtos import (
    DirectoryCounts, DirectoryKind, DirectoryQuery, DirectoryResponse, FileInfo,
    FileListMembership, FileQuery, PathChild, PathType, FileDescriptor, SortField, SortOrder,
    VideoDetails, PhotoDetails, RenameRequest, RenameResponse, AssignLocationRequest, LocationDto,
    ExifTag, MoveRequest, MoveItemResult, MovePreviewResponse, MkdirRequest, MkdirResponse,
)
from db.database import Database, UndoRenameFailedError
from env.environment import Environment
from fileops import service as fileops_service
from fileops.pathlocks import PathLockedError
from photos.exif import dump_all_exif, render_full_raw
from scanner.scanner import Scanner

FilesApi = APIRouter(prefix='/files')

_env = Environment()

# Full-image endpoint: JPEG-family stills are served as-is; RAW stills return
# their largest embedded preview JPEG. (.insp is JPEG-based → passthrough.)
_FULL_IMAGE_JPEG_EXTS = {'.jpg', '.jpeg', '.insp'}
_FULL_IMAGE_RAW_EXTS = {'.rw2', '.dng'}

# Media-type classification mirroring the frontend's VIDEO_TYPES/PHOTO_TYPES
# (frontend/src/app/models.ts), used both for the `counts` block and the
# `kind` request filter on /files/directory.
_VIDEO_MEDIA_TYPES = {'video', '360_video'}
_PHOTO_MEDIA_TYPES = {'photo', '360_photo'}


def _directory_kind(media_type: str | None) -> DirectoryKind:
    if media_type in _VIDEO_MEDIA_TYPES:
        return DirectoryKind.VIDEO
    if media_type in _PHOTO_MEDIA_TYPES:
        return DirectoryKind.PHOTO
    return DirectoryKind.UNTRACKED


def _count_direct_files(dir_path: Path, hidden: set[str]) -> int | None:
    """Direct, non-hidden file count for a subdirectory (not recursive).
    Cheap by design: os.scandir only, no hashing, no DB access. None if the
    subdirectory can't be read (permissions, race with a delete, ...)."""
    try:
        count = 0
        with os.scandir(dir_path) as it:
            for entry in it:
                if entry.name.startswith('._'):
                    continue
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() not in hidden:
                    count += 1
        return count
    except OSError:
        return None


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
            file_count=_count_direct_files(e, hidden) if e.is_dir() else None,
            duration_tc=(
                tracked[e.name]['duration_tc']
                if e.is_file() and e.name in tracked
                and _directory_kind(tracked[e.name]['media_type']) == DirectoryKind.VIDEO
                else None
            ),
        )
        for e in path.iterdir()
        if not e.name.startswith('._')
        and (e.is_dir() or e.suffix.lower() not in hidden)
    ]

    # Counts for the whole directory — independent of pagination AND of any
    # `kind` filter below, so the frontend's filter-segment labels stay
    # correct no matter which subset is currently paginated/filtered.
    counts = DirectoryCounts(
        directories=sum(1 for e in entries if e.type == PathType.DIRECTORY),
        video=sum(1 for e in entries if e.type == PathType.FILE
                   and _directory_kind(e.media_type) == DirectoryKind.VIDEO),
        photo=sum(1 for e in entries if e.type == PathType.FILE
                   and _directory_kind(e.media_type) == DirectoryKind.PHOTO),
        untracked=sum(1 for e in entries if e.type == PathType.FILE
                      and _directory_kind(e.media_type) == DirectoryKind.UNTRACKED),
    )

    if query.kind is not None:
        entries = [e for e in entries if e.type == PathType.FILE and _directory_kind(e.media_type) == query.kind]

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

    return DirectoryResponse(total=total, page=query.page, page_size=query.page_size, items=items, counts=counts)


def _build_file_info(p: Path, db: Database) -> FileInfo:
    stat = p.stat()
    db_record = db.get_file_by_path(str(p))
    video_details = None
    photo_details = None
    keywords = []
    location = None
    lists = []
    if db_record:
        md5 = db_record['md5_hash']
        media_type = db_record['media_type']
        keywords = db.get_keywords(md5)
        lists = [FileListMembership(**row) for row in db.get_lists_for_file(md5)]
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
        lists=lists,
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


def _fileops_error_to_http(e: Exception) -> HTTPException:
    if isinstance(e, PathLockedError):
        return HTTPException(status_code=409, detail='A scan is running in this folder')
    if isinstance(e, fileops_service.OutsideRootError):
        return HTTPException(status_code=403, detail=str(e))
    if isinstance(e, fileops_service.NotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, (fileops_service.AlreadyExistsError, fileops_service.SidecarConflictError,
                     fileops_service.CrossFilesystemError)):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, (fileops_service.InvalidNameError, fileops_service.SelfMoveError)):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, fileops_service.FileOpError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, UndoRenameFailedError):
        return HTTPException(status_code=500, detail=str(e))
    if isinstance(e, OSError):
        return HTTPException(status_code=500, detail=f'Filesystem error: {e}')
    raise e


def _build_rename_response(p: Path, db: Database) -> RenameResponse:
    if p.is_dir():
        stat = p.stat()
        return RenameResponse(
            name=p.name,
            path=str(p),
            file_extension=None,
            size_bytes=0,
            modified_at=datetime.fromtimestamp(stat.st_mtime),
            tracked=False,
            is_directory=True,
        )
    info = _build_file_info(p, db)
    return RenameResponse(**info.model_dump(), is_directory=False)


@FilesApi.patch('/rename')
def rename_file(request: RenameRequest) -> RenameResponse:
    try:
        new_path = fileops_service.rename_path(request.path, request.new_name)
    except Exception as e:
        raise _fileops_error_to_http(e)

    return _build_rename_response(Path(new_path), Database())


@FilesApi.post('/move/preview')
def preview_move(request: MoveRequest) -> MovePreviewResponse:
    try:
        result = fileops_service.preview_move(request.paths, request.target_directory)
    except Exception as e:
        raise _fileops_error_to_http(e)
    return MovePreviewResponse(file_count=result.file_count, tracked_count=result.tracked_count,
                               sidecars=result.sidecars)


@FilesApi.post('/move')
def move_files(request: MoveRequest) -> list[MoveItemResult]:
    try:
        results = fileops_service.move_paths(request.paths, request.target_directory)
    except Exception as e:
        raise _fileops_error_to_http(e)
    return [MoveItemResult(path=r.path, ok=r.ok, new_path=r.new_path, error=r.error) for r in results]


@FilesApi.post('/mkdir', status_code=201)
def make_directory(request: MkdirRequest) -> MkdirResponse:
    try:
        new_path = fileops_service.mkdir(request.parent, request.name)
    except Exception as e:
        raise _fileops_error_to_http(e)
    return MkdirResponse(path=new_path)


@FilesApi.get('/clip-preview/{md5_hash}')
async def get_clip_preview(md5_hash: str):
    data = Database().get_clip_preview(md5_hash)
    if data is None:
        raise HTTPException(status_code=404, detail='No clip preview found')
    return Response(content=data, media_type='image/jpeg')


@FilesApi.get('/full-image/{md5_hash}')
async def get_full_image(md5_hash: str):
    """Full-resolution still: JPEG files as-is, RAW files as their embedded preview JPEG."""
    rec = Database().get_file_by_hash(md5_hash)
    if rec is None:
        raise HTTPException(status_code=404, detail='File not found')
    if rec['media_type'] not in ('photo', '360_photo'):
        raise HTTPException(status_code=400, detail='Full image is only available for still photos')

    root = Path(_env.get_root_dir())
    p = (Path(rec['directory']) / rec['file_name']).resolve()
    if not p.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not p.exists():
        raise HTTPException(status_code=404, detail='File does not exist on disk')

    ext = (rec['file_extension'] or p.suffix).lower()
    if ext in _FULL_IMAGE_JPEG_EXTS:
        return FileResponse(p, media_type='image/jpeg')
    if ext in _FULL_IMAGE_RAW_EXTS:
        data = render_full_raw(str(p))
        if data is None:
            raise HTTPException(status_code=422, detail='Could not render RAW file')
        return Response(content=data, media_type='image/jpeg')
    raise HTTPException(status_code=400, detail=f'Unsupported still format: {ext}')


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
