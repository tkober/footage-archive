from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.dtos import DirectoryQuery, DirectoryResponse, FileInfo, FileQuery, PathChild, PathType, FileDescriptor, SortField, SortOrder
from db.database import Database
from env.environment import Environment
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

    entries = [
        PathChild(
            name=e.name,
            path=str(e),
            type=PathType.DIRECTORY if e.is_dir() else PathType.FILE,
            file_extension=e.suffix.lower() or None
        )
        for e in path.iterdir()
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

    stat = p.stat()
    db_record = Database().get_file_by_path(str(p))

    return FileInfo(
        name=p.name,
        path=str(p),
        file_extension=p.suffix.lower() or None,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        tracked=db_record is not None,
        md5_hash=db_record['md5_hash'] if db_record else None,
        last_indexed_at=db_record['last_indexed_at'] if db_record else None,
    )


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
