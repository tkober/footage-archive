from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, StrictStr


class ScanningQuery(BaseModel):
    generate_clip_preview: bool = True


class FileQuery(ScanningQuery):
    path: StrictStr


class RediscoverQuery(FileQuery):
    track_new: bool = False


class PathType(str, Enum):
    FILE = 'file'
    DIRECTORY = 'directory'


class SortField(str, Enum):
    NAME = 'name'
    TYPE = 'type'


class SortOrder(str, Enum):
    ASC = 'asc'
    DESC = 'desc'


class DirectoryKind(str, Enum):
    """Server-side counterpart of the frontend's video/photo/untracked
    classification (`VIDEO_TYPES`/`PHOTO_TYPES` in `models.ts`, used by the
    browser's filter segments). `video` = media_type in {video, 360_video},
    `photo` = media_type in {photo, 360_photo}, `untracked` = everything else
    (incl. files with no media_type, i.e. not tracked)."""
    VIDEO = 'video'
    PHOTO = 'photo'
    UNTRACKED = 'untracked'


class DirectoryQuery(BaseModel):
    path: StrictStr
    sort_by: SortField = SortField.NAME
    sort_order: SortOrder = SortOrder.ASC
    dirs_first: bool = True
    kind: Optional[DirectoryKind] = None
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
    # Directory entries only: number of direct, non-hidden files in that
    # subdirectory (not recursive). None if the subdirectory couldn't be read.
    file_count: Optional[int] = None
    # Tracked video files only: VideoDetails.duration_tc (e.g. "00:12:34:10").
    duration_tc: Optional[StrictStr] = None


class DirectoryCounts(BaseModel):
    """Counts for the *whole* directory, independent of pagination and of any
    `kind` filter on this request — same video/photo/untracked classification
    as `DirectoryKind` (hidden extensions already excluded)."""
    directories: int
    video: int
    photo: int
    untracked: int


class DirectoryResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[PathChild]
    counts: DirectoryCounts


class FileDescriptor(PathChild):
    md5_hash: StrictStr


class ConfigResponse(BaseModel):
    root_dir: str
    task_poll_interval_ms: int
    browser_hidden_extensions: list[str]
    google_maps_api_key: str
    google_maps_map_id: str


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
    lens: Optional[str] = None
    focal_length_35mm: Optional[float] = None
    scale_factor_35mm: Optional[float] = None
    field_of_view: Optional[float] = None


class ExifTag(BaseModel):
    group: str
    tag: str
    value: str


class MissingFile(BaseModel):
    md5_hash: StrictStr
    file_name: StrictStr
    directory: StrictStr
    media_type: Optional[StrictStr] = None
    keyword_count: int
    has_location: bool
    list_count: int
    has_preview: bool


class RemoveMissingFilesRequest(BaseModel):
    md5_hashes: List[StrictStr]


class RemoveMissingFilesResponse(BaseModel):
    removed: int
    skipped: int


class ConflictCandidate(BaseModel):
    path: StrictStr
    exists: bool
    source: StrictStr
    found_at: Optional[datetime] = None


class ConflictEntry(BaseModel):
    md5_hash: StrictStr
    file_name: StrictStr
    media_type: Optional[StrictStr] = None
    has_preview: bool
    keyword_count: int
    has_location: bool
    list_count: int
    tracked_path: StrictStr
    tracked_exists: bool
    candidates: List[ConflictCandidate]


class ConflictCountResponse(BaseModel):
    count: int


class ResolveConflictRequest(BaseModel):
    md5_hash: StrictStr
    chosen_path: StrictStr


class ResolveBatchStrategy(str, Enum):
    KEEP_TRACKED = 'keep_tracked'
    USE_CANDIDATE = 'use_candidate'


class ResolveBatchRequest(BaseModel):
    strategy: ResolveBatchStrategy
    md5_hashes: List[StrictStr]


class ResolveBatchSkip(BaseModel):
    md5_hash: StrictStr
    reason: StrictStr


class ResolveBatchResponse(BaseModel):
    resolved: int
    skipped: List[ResolveBatchSkip]


class RenameRequest(BaseModel):
    path: StrictStr
    new_name: StrictStr


class MoveRequest(BaseModel):
    paths: List[StrictStr]
    target_directory: StrictStr


class MoveItemResult(BaseModel):
    path: str
    ok: bool
    new_path: Optional[str] = None
    error: Optional[str] = None


class MovePreviewResponse(BaseModel):
    file_count: int
    tracked_count: int
    sidecars: list[str]


class MkdirRequest(BaseModel):
    parent: StrictStr
    name: StrictStr


class MkdirResponse(BaseModel):
    path: str


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


class MapMember(BaseModel):
    md5_hash: str
    file_name: str
    directory: str
    media_type: Optional[str] = None


class MapPoint(BaseModel):
    latitude: float
    longitude: float
    count: int
    video_count: int
    photo_count: int
    # Single-file fields (meaningful only when count == 1): preview + details link.
    md5_hash: Optional[str] = None
    file_name: Optional[str] = None
    directory: Optional[str] = None
    media_type: Optional[str] = None
    # Member bounding box — feeds the cluster's "open in search" link.
    bbox_west: Optional[float] = None
    bbox_south: Optional[float] = None
    bbox_east: Optional[float] = None
    bbox_north: Optional[float] = None
    # Per-member details for small all-stills clusters (inline thumbnails).
    members: Optional[list[MapMember]] = None


class FileSearchQuery(BaseModel):
    media_types: list[str] = []
    keywords: list[str] = []
    country: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    video_codec: Optional[str] = None
    bbox_west: Optional[float] = None
    bbox_south: Optional[float] = None
    bbox_east: Optional[float] = None
    bbox_north: Optional[float] = None
    list_ids: list[int] = []
    list_code: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class SearchResult(BaseModel):
    md5_hash: str
    file_name: str
    directory: str
    media_type: Optional[str]
    recorded_at: Optional[str]
    country: Optional[str]
    city: Optional[str]
    item_code: Optional[str] = None


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SearchResult]


class ListDto(BaseModel):
    id: int
    name: str
    created_at: Optional[datetime] = None
    item_count: int = 0


class CreateListRequest(BaseModel):
    name: StrictStr


class RenameListRequest(BaseModel):
    name: StrictStr


class ListItemDto(BaseModel):
    item_code: str
    md5_hash: str
    file_name: str
    directory: str
    media_type: Optional[str] = None
    added_at: Optional[datetime] = None


class ListItemsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ListItemDto]


class AddFilesToListRequest(BaseModel):
    md5_hashes: list[StrictStr]


class AddFilesToListResponse(BaseModel):
    added: list[ListItemDto]
    existing: list[ListItemDto]
    unknown: list[str]


class FileListMembership(BaseModel):
    list_id: int
    name: str
    item_code: str


class FileInfo(BaseModel):
    name: str
    path: str
    file_extension: Optional[str]
    size_bytes: int
    modified_at: datetime
    tracked: bool
    md5_hash: Optional[str] = None
    media_type: Optional[str] = None
    last_indexed_at: Optional[datetime] = None
    video_details: Optional[VideoDetails] = None
    photo_details: Optional[PhotoDetails] = None
    keywords: list[str] = []
    location: Optional[LocationDto] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    lists: list[FileListMembership] = []


class RenameResponse(FileInfo):
    """FileInfo-compatible rename result — works for both files and
    directories. Existing frontend code only renames files today and reads
    this exactly like a FileInfo (name/path/tracked/...); directories simply
    report tracked=False and no media-specific details, plus an extra
    `is_directory` flag the frontend can ignore for now."""
    is_directory: bool = False
