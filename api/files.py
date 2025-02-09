from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from api.dtos import FileQuery, PathType, PathChild, FileDescriptor
from scanner.scanner import Scanner

FilesApi = APIRouter(prefix='/files')


@FilesApi.post('/directory')
async def query_directory(query: FileQuery) -> List[PathChild]:
    path = Path(query.path)

    return [
        PathChild(
            name=e.name,
            path=str(e),
            type=PathType.DIRECTORY if e.is_dir() else PathType.FILE,
            file_extension=e.suffix
        )
        for e in path.iterdir()
    ]


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
