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
