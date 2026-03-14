export interface Config {
  root_dir: string;
}

export type PathType = 'file' | 'directory';

export interface PathChild {
  name: string;
  path: string;
  type: PathType;
  file_extension: string | null;
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

export interface FileInfo {
  name: string;
  path: string;
  file_extension: string | null;
  size_bytes: number;
  modified_at: string;
  tracked: boolean;
  md5_hash: string | null;
  last_indexed_at: string | null;
}
