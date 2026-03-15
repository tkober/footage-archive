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
    tracked: Optional[bool] = None
    md5_hash: Optional[StrictStr] = None
    media_type: Optional[StrictStr] = None


class DirectoryResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[PathChild]


class FileDescriptor(PathChild):
    md5_hash: StrictStr


class ConfigResponse(BaseModel):
    root_dir: str
    task_poll_interval_ms: int
    browser_hidden_extensions: list[str]


class VideoDetails(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None
    duration_tc: Optional[str] = None
    frame_rate: Optional[float] = None
    frame_rate_verbose: Optional[str] = None
    video_codec: Optional[str] = None
    bit_depth: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_sample_rate: Optional[int] = None
    audio_channels: Optional[int] = None
    audio_bit_depth: Optional[int] = None


class PhotoDetails(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    iso: Optional[int] = None
    aperture: Optional[float] = None
    shutter_speed: Optional[str] = None
    focal_length: Optional[float] = None
    color_space: Optional[str] = None
    bit_depth: Optional[int] = None


class RenameRequest(BaseModel):
    path: StrictStr
    new_name: StrictStr


class KeywordRequest(BaseModel):
    md5_hash: StrictStr
    keyword: StrictStr


class LocationDto(BaseModel):
    id: int
    name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class CreateLocationRequest(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class AssignLocationRequest(BaseModel):
    md5_hash: StrictStr
    location_id: Optional[int] = None


class MapPoint(BaseModel):
    latitude: float
    longitude: float
    count: int
    video_count: int
    photo_count: int
    md5_hash: Optional[str] = None
    file_name: Optional[str] = None
    media_type: Optional[str] = None


class FileInfo(BaseModel):
    name: str
    path: str
    file_extension: Optional[str]
    size_bytes: int
    modified_at: str
    tracked: bool
    md5_hash: Optional[str] = None
    media_type: Optional[str] = None
    last_indexed_at: Optional[str] = None
    video_details: Optional[VideoDetails] = None
    photo_details: Optional[PhotoDetails] = None
    keywords: list[str] = []
    location: Optional[LocationDto] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
