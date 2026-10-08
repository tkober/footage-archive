from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, StrictStr

from tasks.activity import Activity


class ScanningQuery(BaseModel):
    generate_clip_preview: bool = True


class FileQuery(ScanningQuery):
    path: StrictStr
    # Incremental scan (#136): skip a candidate already tracked at exactly
    # this path whose size+mtime are unchanged, without hashing it. True
    # forces every candidate to be hashed+probed regardless. Only honored by
    # POST /tracking/scan-plan and /scan-directory (#138, carried onto every
    # planned unit's ScanJobs.options and applied by run_scan_unit) —
    # scan-file always hashes the one file the user explicitly asked for,
    # and RediscoverQuery (which extends this) always hashes too, since
    # rediscovering by hash is the whole point — so the field is simply
    # unused on those two paths.
    force_rehash: bool = False


class RediscoverQuery(FileQuery):
    track_new: bool = False


class ScanPlanQuery(FileQuery):
    """POST /tracking/scan-plan|scan-directory only (#139, "Scan untracked
    only") — not on FileQuery itself since scan-file/rediscover/
    import-metadata never read this field. True keeps only the walked
    directories where the walk's own media_file_count minus the one
    tracked-count query it already runs is > 0 — see `_walk_and_plan`'s
    docstring for why that's used instead of a stored DirectoryStats row."""
    only_untracked: bool = False


class RefreshQuery(BaseModel):
    """POST /tracking/refresh (#64) — rescan already-tracked files by hash:
    re-probe + regenerate preview without re-hashing. Preview generation is
    always on for a rescan (unlike FileQuery/RediscoverQuery, there's no
    generate_clip_preview toggle)."""
    md5_hashes: List[StrictStr]


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
    # File extension filter (#72), e.g. ".rw2" — normalised server-side
    # (lowercased, leading dot added if missing). When set, only files whose
    # file_extension matches are returned (directories are dropped), AND-ed
    # with `kind` if that's also set.
    extension: Optional[StrictStr] = None
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
    # Directory entries only (#134), for the browser's untracked badge — all
    # three None for a file entry, and all three None (not 0) if the
    # subdirectory couldn't be read, same as `file_count`. `media_file_count`
    # is the direct, non-hidden, non-trash file count whose extension is one
    # of `Environment.get_scanning_file_extensions()` (sidecars like `.xmp`
    # and unrelated files like `.txt` don't count); `tracked_file_count` is
    # how many of those are already tracked (`Files.directory`, exact-string
    # match); `untracked_file_count` is `max(media - tracked, 0)`.
    media_file_count: Optional[int] = None
    tracked_file_count: Optional[int] = None
    untracked_file_count: Optional[int] = None
    # Directory entries only (#139), from this folder's DirectoryStats row —
    # None whenever there's no row (never walked by a scan/census/fileops
    # op yet), not 0; see api/files.py::query_directory for exactly how each
    # is derived.
    # subtree_untracked_count = max(row.subtree_media_files - row.subtree_tracked_files, 0):
    # untracked anywhere at or below this folder.
    subtree_untracked_count: Optional[int] = None
    # below_untracked_count = subtree_untracked_count minus this folder's OWN
    # untracked (both from the row's own media_files/tracked_files, so they
    # share one snapshot) — what the "N below" badge shows. A folder with no
    # real subdirectories is always 0 here, even with no row at all: there's
    # nothing below it to be unknown about.
    below_untracked_count: Optional[int] = None
    # 'complete' (row exists and subtree_complete), 'partial' (row exists,
    # not complete), 'unknown' (no row) — except a folder with no real
    # subdirectories is always 'complete' regardless of whether it has a row.
    subtree_status: Optional[StrictStr] = None
    # DirectoryStats.walked_at for this folder's own row — the "Status as of
    # …" badge tooltip; None with no row.
    status_walked_at: Optional[datetime] = None
    # Tracked video files only: VideoDetails.duration_tc (e.g. "00:12:34:10").
    duration_tc: Optional[StrictStr] = None
    # Derived preview status (#77) — None for untracked/non-media files; see
    # api/preview_status.py::derive_preview_status.
    preview_status: Optional[StrictStr] = None


class DirectoryCounts(BaseModel):
    """Counts for the *whole* directory, independent of pagination and of any
    `kind` filter on this request — same video/photo/untracked classification
    as `DirectoryKind` (hidden extensions already excluded).

    `extensions` (#72) is different: it's a per-file-extension breakdown
    (e.g. {".rw2": 12, ".mp4": 3}) taken *after* the `kind` filter but
    *before* the `extension` filter, so the file-type dropdown only offers
    extensions that match the currently selected kind, and its counts don't
    collapse to just the chosen extension once one is picked. Files with no
    extension are skipped. Keys include the leading dot."""
    directories: int
    video: int
    photo: int
    untracked: int
    extensions: dict[str, int] = {}


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
    google_maps_map_id_poi: str
    trash_dir_name: str


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


class DeleteRequest(BaseModel):
    paths: List[StrictStr]


class DeletePreviewResponse(BaseModel):
    file_count: int
    tracked_count: int
    sidecars: list[str]
    list_item_count: int
    keyword_count: int


class DeleteItemResult(BaseModel):
    path: str
    ok: bool
    trash_path: Optional[str] = None
    untracked_count: Optional[int] = None
    error: Optional[str] = None


class DeleteBatchResponse(BaseModel):
    trash_batch: str
    results: list[DeleteItemResult]


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
    # Date range (EXIF text, "YYYY:MM:DD HH:MM:SS") spanning every member;
    # None if no member in the cluster has a recorded_at.
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    # Most common place name among members (city, falling back to country);
    # None if no member has a location.
    place: Optional[str] = None
    # Per-member preview (up to 7, newest first) for every cluster — small
    # clusters can show each photo inline, larger ones a representative sample.
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
    preview_status: Optional[str] = None


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
    preview_status: Optional[str] = None


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
    # Derived preview status + the PreviewStatus row behind it (#77); see
    # api/preview_status.py::derive_preview_status. All None for
    # untracked/non-media files.
    preview_status: Optional[str] = None
    preview_error: Optional[str] = None
    preview_attempted_at: Optional[datetime] = None


class RenameResponse(FileInfo):
    """FileInfo-compatible rename result — works for both files and
    directories. Existing frontend code only renames files today and reads
    this exactly like a FileInfo (name/path/tracked/...); directories simply
    report tracked=False and no media-specific details, plus an extra
    `is_directory` flag the frontend can ignore for now."""
    is_directory: bool = False


class LoadAvg(BaseModel):
    """os.getloadavg() — 1/5/15 minute averages."""
    load_1m: float
    load_5m: float
    load_15m: float


class LastSlowJob(BaseModel):
    label: str
    duration_s: float


class SystemDiagnostics(BaseModel):
    """Live runtime snapshot from tasks/loadcontrol.py::diagnostics() (#71)."""
    cpu_count: Optional[int] = None
    cpu_limit: Optional[float] = None
    load_avg: LoadAvg
    cpu_usage_percent: Optional[float] = None
    cpu_temperature_c: Optional[float] = None
    throttled: bool
    throttle_reason: Optional[str] = None
    active_heavy_jobs: int
    waiting_heavy_jobs: int
    heavy_jobs_total: int
    heavy_jobs_seconds_total: float
    throttle_events: int
    last_slow_job: Optional[LastSlowJob] = None
    pool_queue_length: Optional[int] = None


class SystemSettings(BaseModel):
    """Effective load-management settings — read-only on the Settings page
    for now (#71); editing them is a follow-up)."""
    worker_pool_size: int
    db_pool_size: int
    db_max_overflow: int
    heavy_job_concurrency: int
    ffmpeg_threads: int
    process_niceness: int
    cpu_temp_limit_c: float
    load_avg_limit: float


class SystemDiagnosticsResponse(BaseModel):
    settings: SystemSettings
    runtime: SystemDiagnostics


class ScanJobOptions(BaseModel):
    """`ScanJobs.options` — applied to every unit's scan (#137)."""
    generate_clip_preview: bool = True
    force_rehash: bool = False


class ScanUnitResult(BaseModel):
    """`ScanUnits.result` once a unit is terminal — the same counts as
    api/tracking.py's ScanSummary (minus `cancelled`, which is the unit's
    own `status` here)."""
    indexed: int = 0
    relinked: int = 0
    conflicts: int = 0
    failed: int = 0
    skipped: int = 0


class ScanUnitDto(BaseModel):
    id: int
    directory: StrictStr
    position: int
    status: StrictStr
    media_file_count: Optional[int] = None
    tracked_file_count: Optional[int] = None
    progress: Optional[str] = None
    error: Optional[str] = None
    result: Optional[ScanUnitResult] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Live, like TaskDescription.activity — only set for a RUNNING unit this
    # process actually claimed (tasks/scanqueue.py).
    activity: Optional[Activity] = None


class ScanJobDto(BaseModel):
    id: StrictStr
    root_path: StrictStr
    status: StrictStr
    options: ScanJobOptions
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None
    units: List[ScanUnitDto] = []


class CensusQuery(BaseModel):
    """POST /tracking/census (#139) — "Refresh status": re-walks `path` and
    rewrites every DirectoryStats row under it. Just the path — unlike a
    scan, a census never hashes anything, so none of FileQuery's scanning
    options apply."""
    path: StrictStr


class ScanPlanSkip(BaseModel):
    """One directory `POST /tracking/scan-plan` (#138) left out of the plan
    because it's already QUEUED/RUNNING in another job right now."""
    directory: StrictStr
    reason: StrictStr


class ScanPlanResponse(ScanJobDto):
    """`POST /tracking/scan-plan`'s response (#138): the PLANNED job exactly
    like `ScanJobDto`, plus the directories the walk found but didn't turn
    into a unit because another job already has them QUEUED/RUNNING."""
    skipped: List[ScanPlanSkip] = []


class ScanJobListEntry(BaseModel):
    """GET /scan-jobs's shape — aggregated progress instead of the full unit
    list (see Database.get_scan_jobs for the units_done/_total,
    files_done/_total definitions)."""
    id: StrictStr
    root_path: StrictStr
    status: StrictStr
    options: ScanJobOptions
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None
    units_done: int
    units_total: int
    files_done: int
    files_total: int
