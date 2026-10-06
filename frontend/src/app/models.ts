export interface Config {
  root_dir: string;
  task_poll_interval_ms: number;
  browser_hidden_extensions: string[];
  google_maps_api_key: string;
  google_maps_map_id: string;
  /** Single folder name under `root_dir` that delete-to-trash moves files into (#61). */
  trash_dir_name: string;
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

/** Derived preview status (#77) for a tracked, previewable file:
    - 'ok' (or absent, for backwards-compat): preview ready, render as today.
    - 'generating': a scan/rescan/repair task has it queued or in flight right now.
    - 'missing': never attempted.
    - 'failed': attempted, but the preview generator errored.
    - 'unsupported': attempted, but the format isn't one the preview
      generator can read at all (e.g. an Insta360 .dng).
    Untracked or non-media files carry no `preview_status` at all. */
export type PreviewStatus = 'ok' | 'generating' | 'missing' | 'failed' | 'unsupported';

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
  preview_status?: PreviewStatus | null;
}

/** Counts for the whole directory (#46) — independent of pagination and of
    any `kind` filter on the request, same video/photo/untracked
    classification as `VIDEO_TYPES`/`PHOTO_TYPES` below. */
export interface DirectoryCounts {
  directories: number;
  video: number;
  photo: number;
  untracked: number;
  /** Per-file-extension breakdown (#72), e.g. {".rw2": 12, ".mp4": 3} — taken
      after the `kind` filter but before the `extension` filter, so the file
      type dropdown only offers extensions matching the current kind and its
      counts don't collapse once an extension is chosen. Keys include the
      leading dot; files without an extension are skipped. */
  extensions: Record<string, number>;
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
  /** Server-side file extension filter (#72), e.g. ".rw2" — normalised on
      the backend (lowercased, leading dot added if missing). Only matching
      files are returned (no directories); combinable with `kind` (AND), and
      total/pagination refer to the filtered list. */
  extension?: string | null;
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
  preview_status?: PreviewStatus | null;
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
  preview_status?: PreviewStatus | null;
  preview_error?: string | null;
  preview_attempted_at?: string | null;
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

/** Dry-run counts for a delete-to-trash, from POST /files/delete/preview (#61).
    Purely informational — never mutates anything. `list_item_count`/`keyword_count`
    are only meaningful (and only shown) when `tracked_count > 0`. */
export interface DeletePreviewResponse {
  file_count: number;
  tracked_count: number;
  sidecars: string[];
  list_item_count: number;
  keyword_count: number;
}

/** Per-path outcome of POST /files/delete (bulk-safe: one entry per requested path). */
export interface DeleteItemResult {
  path: string;
  ok: boolean;
  trash_path?: string | null;
  untracked_count?: number | null;
  error?: string | null;
}

export interface DeleteBatchResponse {
  trash_batch: string;
  results: DeleteItemResult[];
}

/** Confirm-dialog copy for a delete-to-trash preview (#61), shared by the
    browser's context menu/bulk delete and the file detail panel's own
    delete action so both read identically. */
export function formatDeletePreview(
  preview: DeletePreviewResponse,
  rootDir: string,
  trashDirName: string,
): { message: string; warning: string | null } {
  const fileWord = preview.file_count === 1 ? 'file' : 'files';
  let message = `${preview.file_count} ${fileWord}`;
  if (preview.sidecars.length) {
    // file_count already includes the sidecars
    message += ` (incl. ${preview.sidecars.length} sidecar${preview.sidecars.length === 1 ? '' : 's'})`;
  }
  const dest = [rootDir, trashDirName].filter(Boolean).join('/');
  message += ` will be moved to "${dest}/…", where they can be restored manually (without tracking).`;

  let warning: string | null = null;
  if (preview.tracked_count > 0) {
    const trackedWord = preview.tracked_count === 1 ? 'file' : 'files';
    const extras: string[] = [];
    if (preview.keyword_count > 0) extras.push(`${preview.keyword_count} keyword${preview.keyword_count === 1 ? '' : 's'}`);
    if (preview.list_item_count > 0) extras.push(`${preview.list_item_count} list entr${preview.list_item_count === 1 ? 'y' : 'ies'}`);
    warning = `${preview.tracked_count} tracked ${trackedWord} will permanently lose their keywords, location and list entries`
      + (extras.length ? ` (${extras.join(', ')})` : '') + '.';
  }
  return { message, warning };
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
  preview_status?: PreviewStatus | null;
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

/** GET /system/diagnostics (#71) — read-only load-management settings +
    live runtime diagnostics, shown on the Settings page's "Performance" section. */
export interface SystemSettings {
  worker_pool_size: number;
  db_pool_size: number;
  db_max_overflow: number;
  heavy_job_concurrency: number;
  ffmpeg_threads: number;
  process_niceness: number;
  cpu_temp_limit_c: number;
  load_avg_limit: number;
}

export interface LoadAvg {
  load_1m: number;
  load_5m: number;
  load_15m: number;
}

export interface LastSlowJob {
  label: string;
  duration_s: number;
}

export interface SystemDiagnostics {
  cpu_count: number | null;
  cpu_limit: number | null;
  load_avg: LoadAvg;
  cpu_usage_percent: number | null;
  cpu_temperature_c: number | null;
  throttled: boolean;
  throttle_reason: string | null;
  active_heavy_jobs: number;
  waiting_heavy_jobs: number;
  heavy_jobs_total: number;
  heavy_jobs_seconds_total: number;
  throttle_events: number;
  last_slow_job: LastSlowJob | null;
  pool_queue_length: number | null;
}

export interface SystemDiagnosticsResponse {
  settings: SystemSettings;
  runtime: SystemDiagnostics;
}
