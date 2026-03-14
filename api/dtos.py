from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, StrictStr


class ScanningQuery(BaseModel):
    generate_clip_preview: bool = True


class FileQuery(ScanningQuery):
    path: StrictStr


class PathType(str, Enum):
    FILE = 'file'
    DIRECTORY = 'directory'


class SortField(str, Enum):
    NAME = 'name'
    TYPE = 'type'


class SortOrder(str, Enum):
    ASC = 'asc'
    DESC = 'desc'


class DirectoryQuery(BaseModel):
    path: StrictStr
    sort_by: SortField = SortField.NAME
    sort_order: SortOrder = SortOrder.ASC
    dirs_first: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)


class PathChild(BaseModel):
    name: StrictStr
    path: StrictStr
    type: PathType
    file_extension: Optional[StrictStr]


class DirectoryResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[PathChild]


class FileDescriptor(PathChild):
    md5_hash: StrictStr
