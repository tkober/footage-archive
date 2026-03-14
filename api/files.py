from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.dtos import DirectoryQuery, DirectoryResponse, FileQuery, PathChild, PathType, FileDescriptor, SortField, SortOrder
from scanner.scanner import Scanner

FilesApi = APIRouter(prefix='/files')


@FilesApi.post('/directory')
async def query_directory(query: DirectoryQuery) -> DirectoryResponse:
    path = Path(query.path)

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
