export interface Config {
  root_dir: string;
  task_poll_interval_ms: number;
  browser_hidden_extensions: string[];
  google_maps_api_key: string;
  google_maps_map_id: string;
}

export type TaskStatus = 'PENDING' | 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED';

export interface Task {
  id: string;
  name: string;
  description: string;
  status: TaskStatus;
  scheduled_at: string | null;
  started_at: string | null;
  last_updated: string;
  error: string | null;
  progress: string | null;
}

export type PathType = 'file' | 'directory';

export interface PathChild {
  name: string;
  path: string;
  type: PathType;
  file_extension: string | null;
  tracked: boolean | null;
  md5_hash?: string | null;
  media_type?: MediaType | null;
  /** Directory entries only: number of direct, non-hidden files in that
      subdirectory (not recursive). Null if the subdirectory couldn't be read. */
  file_count?: number | null;
  /** Tracked video files only (#39): `HH:MM:SS:FF` from `VideoDetails.duration_tc`,
      loaded in the same directory-listing query. Null for photos/untracked/directories. */
  duration_tc?: string | null;
}

/** Counts for the whole directory (#46) — independent of pagination and of
    any `kind` filter on the request, same video/photo/untracked
    classification as `VIDEO_TYPES`/`PHOTO_TYPES` below. */
export interface DirectoryCounts {
  directories: number;
  video: number;
  photo: number;
  untracked: number;
}

export interface DirectoryResponse {
  total: number;
  page: number;
  page_size: number;
  items: PathChild[];
  counts: DirectoryCounts;
}

export type DirectoryKind = 'video' | 'photo' | 'untracked';

export interface DirectoryQuery {
  path: string;
  sort_by?: 'name' | 'type';
  sort_order?: 'asc' | 'desc';
  dirs_first?: boolean;
  /** Server-side filter (#46): only matching files are returned (no
      directories), and total/pagination refer to the filtered list.
      Omitted (default) = everything, unchanged behaviour. */
  kind?: DirectoryKind | null;
  page?: number;
  page_size?: number;
}

export type MediaType = 'video' | 'photo' | '360_video' | '360_photo';

export const VIDEO_TYPES: MediaType[] = ['video', '360_video'];
export const PHOTO_TYPES: MediaType[] = ['photo', '360_photo'];

/** `PathChild.duration_tc` (#39) formatted for the media card's caption:
    `HH:MM:SS:FF` → `mm:ss`, or `h:mm:ss` once the clip runs an hour or
    longer. Returns null for anything that doesn't parse. */
export function formatDurationTc(tc: string | null | undefined): string | null {
  if (!tc) return null;
  const parts = tc.split(':').map(Number);
  if (parts.length < 3 || parts.slice(0, 3).some(n => !Number.isFinite(n))) return null;
  const [h, m, s] = parts;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h >= 1 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export interface VideoDetails {
  width: number | null;
  height: number | null;
  duration_tc: string | null;
  frame_rate: number | null;
  frame_rate_verbose: string | null;
  video_codec: string | null;
  bit_depth: number | null;
  audio_codec: string | null;
  audio_sample_rate: number | null;
  audio_channels: number | null;
  audio_bit_depth: number | null;
}

export interface PhotoDetails {
  width: number | null;
  height: number | null;
  camera_make: string | null;
  camera_model: string | null;
  iso: number | null;
  aperture: number | null;
  shutter_speed: string | null;
  focal_length: number | null;
  color_space: string | null;
  bit_depth: number | null;
  lens: string | null;
  focal_length_35mm: number | null;
  scale_factor_35mm: number | null;
  field_of_view: number | null;
}

export interface Location {
  id: number;
  name?: string | null;
  city?: string | null;
  region?: string | null;
  country?: string | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface FileSearchQuery {
  media_types?: string[];
  keywords?: string[];
  country?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  camera_make?: string | null;
  camera_model?: string | null;
  video_codec?: string | null;
  bbox_west?: number | null;
  bbox_south?: number | null;
  bbox_east?: number | null;
  bbox_north?: number | null;
  list_ids?: number[];
  list_code?: string | null;
  page?: number;
  page_size?: number;
}

export interface SearchResult {
  md5_hash: string;
  file_name: string;
  directory: string;
  media_type: string | null;
  recorded_at: string | null;
  country: string | null;
  city: string | null;
  item_code?: string | null;
}

export interface SearchResponse {
  total: number;
  page: number;
  page_size: number;
  items: SearchResult[];
}

export interface MapMember {
  md5_hash: string;
  file_name: string;
  directory: string;
  media_type: string | null;
}

export interface MapPoint {
  latitude: number;
  longitude: number;
  count: number;
  video_count: number;
  photo_count: number;
  // Single-file fields (meaningful only when count === 1)
  md5_hash: string | null;
  file_name: string | null;
  directory: string | null;
  media_type: string | null;
  // Member bounding box — feeds the cluster's "open in search" link
  bbox_west: number | null;
  bbox_south: number | null;
  bbox_east: number | null;
  bbox_north: number | null;
  // Per-member details for small all-stills clusters (inline thumbnails)
  members: MapMember[] | null;
}

export interface FileListMembership {
  list_id: number;
  name: string;
  item_code: string;
}

export interface FileInfo {
  name: string;
  path: string;
  file_extension: string | null;
  size_bytes: number;
  modified_at: string;
  tracked: boolean;
  md5_hash: string | null;
  media_type: MediaType | null;
  last_indexed_at: string | null;
  video_details?: VideoDetails | null;
  photo_details?: PhotoDetails | null;
  keywords?: string[];
  location?: Location | null;
  latitude?: number | null;
  longitude?: number | null;
  altitude?: number | null;
  lists?: FileListMembership[];
}

/** PATCH /files/rename response: a FileInfo (zeroed-out for directories) plus
    a flag telling the caller whether the renamed path was a directory. */
export interface RenameResponse extends FileInfo {
  is_directory: boolean;
}

/** Dry-run counts for a move/rename, from POST /files/move/preview. Purely
    informational — never mutates anything. */
export interface MovePreviewResponse {
  file_count: number;
  tracked_count: number;
  sidecars: string[];
}

/** Per-path outcome of POST /files/move (bulk-safe: one entry per requested path). */
export interface MoveItemResult {
  path: string;
  ok: boolean;
  new_path?: string | null;
  error?: string | null;
}

export interface MkdirResponse {
  path: string;
}

export interface FileList {
  id: number;
  name: string;
  created_at: string | null;
  item_count: number;
}

export interface ListItem {
  item_code: string;
  md5_hash: string;
  file_name: string;
  directory: string;
  media_type: string | null;
  added_at: string | null;
}

export interface ListItemsResponse {
  total: number;
  page: number;
  page_size: number;
  items: ListItem[];
}

export interface AddFilesToListResponse {
  added: ListItem[];
  existing: ListItem[];
  unknown: string[];
}

export interface ShotMovement {
  movement_type: string;
  movement_direction: string;
  movement_intensity: string;
  zoom: string;
  zoom_intensity: string;
}

export interface ExifTag {
  group: string;
  tag: string;
  value: string;
}

export interface MissingFile {
  md5_hash: string;
  file_name: string;
  directory: string;
  media_type: MediaType | null;
  keyword_count: number;
  has_location: boolean;
  list_count: number;
  has_preview: boolean;
}

export interface RemoveMissingFilesResponse {
  removed: number;
  skipped: number;
}

export interface ConflictCandidate {
  path: string;
  exists: boolean;
  source: string;
  found_at: string | null;
}

export interface ConflictEntry {
  md5_hash: string;
  file_name: string;
  media_type: MediaType | null;
  has_preview: boolean;
  keyword_count: number;
  has_location: boolean;
  list_count: number;
  tracked_path: string;
  tracked_exists: boolean;
  candidates: ConflictCandidate[];
}

export type ResolveBatchStrategy = 'keep_tracked' | 'use_candidate';

export interface ResolveBatchSkip {
  md5_hash: string;
  reason: string;
}

export interface ResolveBatchResponse {
  resolved: number;
  skipped: ResolveBatchSkip[];
}

export interface ShotFraming {
  shot_size: string;
  angle: string;
  composition: string;
}

export interface ShotScene {
  location_type: string;
  environment: string;
  subjects: string[];
  activity: string;
}

export interface ShotVisual {
  time_of_day: string;
  lighting_type: string;
  lighting_style: string;
  color_tone: string;
  mood: string;
}

export interface ShotTechnical {
  camera_motion_vector: string;
  stability: string;
}

export interface ShotClassification {
  movement: ShotMovement;
  framing: ShotFraming;
  scene: ShotScene;
  visual: ShotVisual;
  technical: ShotTechnical;
}
