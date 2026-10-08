import mimetypes
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
    DeleteRequest, DeletePreviewResponse, DeleteBatchResponse, DeleteItemResult,
)
from api.preview_status import derive_preview_status
from db.database import Database, UndoRenameFailedError
from env.environment import Environment
from env.hidden_files import is_hidden_system_file
from fileops import service as fileops_service
from fileops.pathlocks import PathLockedError
from fileops.trash import is_in_trash
from photos.exif import RAW_EXTENSIONS, dump_all_exif, render_full_raw
from scanner.scanner import Scanner

FilesApi = APIRouter(prefix='/files')

_env = Environment()

# Full-image endpoint: JPEG-family stills are served as-is; RAW stills
# (RAW_EXTENSIONS, #78) get a full-resolution rawpy render (render_full_raw).
# (.insp is JPEG-based → passthrough.)
_FULL_IMAGE_JPEG_EXTS = {'.jpg', '.jpeg', '.insp'}

# Media-type classification mirroring the frontend's VIDEO_TYPES/PHOTO_TYPES
# (frontend/src/app/models.ts), used both for the `counts` block and the
# `kind` request filter on /files/directory.
_VIDEO_MEDIA_TYPES = {'video', '360_video'}
_PHOTO_MEDIA_TYPES = {'photo', '360_photo'}

# Stream endpoint (#109): content types for the original-video extensions we
# track (env MEDIA_TYPE_VIDEO); anything else falls back to mimetypes.guess_type,
# then to a generic binary type.
_VIDEO_CONTENT_TYPES = {
    '.mov': 'video/quicktime',
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
}


def _directory_kind(media_type: str | None) -> DirectoryKind:
    if media_type in _VIDEO_MEDIA_TYPES:
        return DirectoryKind.VIDEO
    if media_type in _PHOTO_MEDIA_TYPES:
        return DirectoryKind.PHOTO
    return DirectoryKind.UNTRACKED


def _normalize_extension(extension: str | None) -> str | None:
    """Normalise a DirectoryQuery.extension value (#72): lowercase, ensure a
    leading dot. None/blank stays None (no filter)."""
    if not extension:
        return None
    extension = extension.strip().lower()
    if not extension:
        return None
    if not extension.startswith('.'):
        extension = '.' + extension
    return extension


def _count_direct_files(
    dir_path: Path, hidden_extensions: set[str], hidden_names: set[str], scanning_extensions: set[str],
) -> tuple[int | None, int | None, bool | None]:
    """Direct, non-hidden file count for a subdirectory (not recursive), how
    many of those are "relevant" media files (#134: lowercase extension in
    `scanning_extensions`, not hidden — the pool the browser's untracked
    badge is counted against), and whether the subdirectory has any real
    (non-hidden) subdirectory of its own (#139 — a folder with none is
    always "complete" for the "N below" badge, even with no DirectoryStats
    row at all, since there's nothing below it to be unknown about). No
    per-file trash check: the caller already skips `dir_path` itself when
    it is (inside) the trash, so none of its direct entries can be. All
    three numbers come from the same `os.scandir` pass, so this stays one
    scandir per child folder regardless of how many of them it returns.
    Cheap by design: no hashing, no DB access. All three None if the
    subdirectory can't be read (permissions, race with a delete, ...)."""
    try:
        count = 0
        relevant = 0
        has_subdirs = False
        with os.scandir(dir_path) as it:
            for entry in it:
                if is_hidden_system_file(entry.name, hidden_extensions, hidden_names):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    has_subdirs = True
                    continue
                if not entry.is_file():
                    continue
                count += 1
                if os.path.splitext(entry.name)[1].lower() in scanning_extensions:
                    relevant += 1
        return count, relevant, has_subdirs
    except OSError:
        return None, None, None


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
    hidden_names = set(_env.get_browser_hidden_names())
    scanning_extensions = set(_env.get_scanning_file_extensions())
    db = Database()
    tracked = db.get_tracked_files_in_directory(str(path))

    entries = []
    has_subdirs_by_path: dict[str, bool | None] = {}
    for e in path.iterdir():
        if e.name.startswith('._'):
            continue
        if e.is_file() and is_hidden_system_file(e.name, hidden, hidden_names):
            continue
        if is_in_trash(e):
            continue
        # One scandir per child folder (#134/#139): file_count/media_file_count
        # and whether it has any subdirectory of its own all come out of the
        # same _count_direct_files call.
        file_count, media_file_count, has_subdirs = _count_direct_files(e, hidden, hidden_names, scanning_extensions) \
            if e.is_dir() else (None, None, None)
        has_subdirs_by_path[str(e)] = has_subdirs
        entries.append(PathChild(
            name=e.name,
            path=str(e),
            type=PathType.DIRECTORY if e.is_dir() else PathType.FILE,
            file_extension=e.suffix.lower() or None,
            tracked=e.name in tracked if e.is_file() else None,
            md5_hash=tracked[e.name]['md5_hash'] if e.is_file() and e.name in tracked else None,
            media_type=tracked[e.name]['media_type'] if e.is_file() and e.name in tracked else None,
            file_count=file_count,
            media_file_count=media_file_count,
            duration_tc=(
                tracked[e.name]['duration_tc']
                if e.is_file() and e.name in tracked
                and _directory_kind(tracked[e.name]['media_type']) == DirectoryKind.VIDEO
                else None
            ),
            preview_status=(
                derive_preview_status(
                    tracked[e.name]['md5_hash'], tracked[e.name]['media_type'],
                    tracked[e.name]['has_preview'], tracked[e.name]['preview_status'],
                )
                if e.is_file() and e.name in tracked
                else None
            ),
        ))

    # Tracked-file count per child folder (#134), one query for every
    # directory entry in this listing — never per child (see
    # Database.count_tracked_files_by_directory).
    directory_paths = [e.path for e in entries if e.type == PathType.DIRECTORY]
    tracked_counts = db.count_tracked_files_by_directory(directory_paths)
    for e in entries:
        if e.type == PathType.DIRECTORY and e.media_file_count is not None:
            e.tracked_file_count = tracked_counts.get(e.path, 0)
            e.untracked_file_count = max(e.media_file_count - e.tracked_file_count, 0)

    # DirectoryStats rows (#139) for every child folder, ONE more query —
    # same convention as count_tracked_files_by_directory just above.
    stats_rows = db.get_directory_stats_batch(directory_paths)
    for e in entries:
        if e.type != PathType.DIRECTORY:
            continue
        row = stats_rows.get(e.path)
        has_subdirs = has_subdirs_by_path.get(e.path)
        if row is not None:
            own_untracked = max((row['media_files'] or 0) - (row['tracked_files'] or 0), 0)
            subtree_untracked = max((row['subtree_media_files'] or 0) - (row['subtree_tracked_files'] or 0), 0)
            e.subtree_untracked_count = subtree_untracked
            e.subtree_media_count = row['subtree_media_files'] or 0
            e.below_untracked_count = max(subtree_untracked - own_untracked, 0)
            e.subtree_status = 'complete' if row['subtree_complete'] else 'partial'
            e.status_walked_at = row['walked_at']
        elif has_subdirs is False:
            # Never walked, but it has no real subdirectory of its own — so
            # there's nothing below it to be unknown about.
            e.subtree_status = 'complete'
            e.below_untracked_count = 0
            e.subtree_media_count = e.media_file_count
        else:
            # has_subdirs is True (real subdirectories exist, but this
            # folder has no DirectoryStats row yet) or None (unreadable) —
            # genuinely unknown; below_untracked_count stays None too.
            e.subtree_status = 'unknown'

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
        extensions={},
    )

    if query.kind is not None:
        entries = [e for e in entries if e.type == PathType.FILE and _directory_kind(e.media_type) == query.kind]

    # Extension breakdown (#72) — taken after the `kind` filter but before
    # the `extension` filter below, per DirectoryCounts' docstring.
    extension_counts: dict[str, int] = {}
    for e in entries:
        if e.type == PathType.FILE and e.file_extension:
            extension_counts[e.file_extension] = extension_counts.get(e.file_extension, 0) + 1
    counts.extensions = extension_counts

    extension = _normalize_extension(query.extension)
    if extension is not None:
        entries = [e for e in entries if e.type == PathType.FILE and e.file_extension == extension]

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
    preview_status = None
    preview_error = None
    preview_attempted_at = None
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
        status_row = db.get_preview_status_row(md5)
        preview_status = derive_preview_status(
            md5, media_type, db.has_clip_preview(md5), status_row['status'] if status_row else None,
        )
        if status_row:
            preview_error = status_row['reason']
            preview_attempted_at = status_row['attempted_at']
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
        preview_status=preview_status,
        preview_error=preview_error,
        preview_attempted_at=preview_attempted_at,
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
def get_file_exif(path: str) -> list[ExifTag]:
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
    if isinstance(e, (fileops_service.InvalidNameError, fileops_service.SelfMoveError,
                     fileops_service.TrashPathError)):
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


@FilesApi.post('/delete/preview')
def preview_delete(request: DeleteRequest) -> DeletePreviewResponse:
    try:
        result = fileops_service.preview_delete(request.paths)
    except Exception as e:
        raise _fileops_error_to_http(e)
    return DeletePreviewResponse(
        file_count=result.file_count, tracked_count=result.tracked_count,
        sidecars=result.sidecars, list_item_count=result.list_item_count,
        keyword_count=result.keyword_count,
    )


@FilesApi.post('/delete')
def delete_files_to_trash(request: DeleteRequest) -> DeleteBatchResponse:
    try:
        result = fileops_service.delete_paths(request.paths)
    except Exception as e:
        raise _fileops_error_to_http(e)
    return DeleteBatchResponse(
        trash_batch=result.trash_batch,
        results=[
            DeleteItemResult(path=r.path, ok=r.ok, trash_path=r.trash_path,
                             untracked_count=r.untracked_count, error=r.error)
            for r in result.results
        ],
    )


@FilesApi.get('/clip-preview/{md5_hash}')
async def get_clip_preview(md5_hash: str):
    data = Database().get_clip_preview(md5_hash)
    if data is None:
        raise HTTPException(status_code=404, detail='No clip preview found')
    return Response(content=data, media_type='image/jpeg')


@FilesApi.get('/full-image/{md5_hash}')
def get_full_image(md5_hash: str):
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
    if ext in RAW_EXTENSIONS:
        data = render_full_raw(str(p))
        if data is None:
            raise HTTPException(status_code=422, detail='Could not render RAW file')
        return Response(content=data, media_type='image/jpeg')
    raise HTTPException(status_code=400, detail=f'Unsupported still format: {ext}')


@FilesApi.get('/stream/{md5_hash}')
def stream_file(md5_hash: str):
    """Original video, streamed with HTTP Range support so the browser can
    seek without downloading the whole file (#109). `360_video` is out of
    scope (no in-browser 360 player yet) and stills have no use here, so
    both are rejected with 400. Starlette's `FileResponse` (0.52.1) already
    handles `Range`/`If-Range`/206/416 and sets `Accept-Ranges: bytes` —
    nothing to reimplement here, see tests/test_files_api.py.
    """
    rec = Database().get_file_by_hash(md5_hash)
    if rec is None:
        raise HTTPException(status_code=404, detail='File not found')
    if rec['media_type'] != 'video':
        raise HTTPException(status_code=400, detail='Streaming is only available for videos')

    root = Path(_env.get_root_dir())
    p = (Path(rec['directory']) / rec['file_name']).resolve()
    if not p.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    # Defensive only: tracked files should never point into the trash, but
    # check anyway rather than ever stream out of it.
    if is_in_trash(p):
        raise HTTPException(status_code=404, detail='File not found')
    if not p.exists():
        raise HTTPException(status_code=404, detail='File does not exist on disk')

    ext = (rec['file_extension'] or p.suffix).lower()
    media_type = _VIDEO_CONTENT_TYPES.get(ext)
    if media_type is None:
        media_type = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    # No `filename=` -> no Content-Disposition, so the browser plays the
    # video inline instead of offering it as a download.
    return FileResponse(p, media_type=media_type)


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
def get_checksum(query: FileQuery) -> FileDescriptor:
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
