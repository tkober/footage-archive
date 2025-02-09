from enum import Enum
from typing import Optional

from pydantic import BaseModel, StrictStr


class FileQuery(BaseModel):
    path: StrictStr = '/Users/kober/Desktop'


class PathType(str, Enum):
    FILE = 'file'
    DIRECTORY = 'directory'


class PathChild(BaseModel):
    name: StrictStr
    path: StrictStr
    type: PathType
    file_extension: Optional[StrictStr]


class FileDescriptor(PathChild):
    md5_hash: StrictStr
