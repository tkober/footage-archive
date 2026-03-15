export interface Config {
  root_dir: string;
  task_poll_interval_ms: number;
  browser_hidden_extensions: string[];
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
}

export interface DirectoryResponse {
  total: number;
  page: number;
  page_size: number;
  items: PathChild[];
}

export interface DirectoryQuery {
  path: string;
  sort_by?: 'name' | 'type';
  sort_order?: 'asc' | 'desc';
  dirs_first?: boolean;
  page?: number;
  page_size?: number;
}

export type MediaType = 'video' | 'photo' | '360_video' | '360_photo';

export const VIDEO_TYPES: MediaType[] = ['video', '360_video'];
export const PHOTO_TYPES: MediaType[] = ['photo', '360_photo'];

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

export interface MapPoint {
  latitude: number;
  longitude: number;
  count: number;
  video_count: number;
  photo_count: number;
  md5_hash: string | null;
  file_name: string | null;
  media_type: string | null;
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
}
