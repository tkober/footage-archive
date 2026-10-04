import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject } from 'rxjs';
import { shareReplay } from 'rxjs/operators';

import { environment } from '../../environments/environment';
import { AddFilesToListResponse, Config, ConflictEntry, DirectoryQuery, DirectoryResponse, ExifTag, FileInfo, FileList, FileSearchQuery, ListItem, ListItemsResponse, Location, MapPoint, MissingFile, MkdirResponse, MoveItemResult, MovePreviewResponse, RenameResponse, ResolveBatchResponse, ResolveBatchStrategy, SearchResponse, ShotClassification, Task } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly base = environment.apiUrl;
  readonly taskRefresh$ = new Subject<void>();
  /** Fired whenever something may have changed the open-conflicts count (a
      resolve, a completed Rediscover task) — the sidebar badge and the
      maintenance page's "Path conflicts" section both listen. */
  readonly conflictsChanged$ = new Subject<void>();
  private config$?: Observable<Config>;

  constructor(private http: HttpClient) {}

  /** Cached: /config is immutable per session and read by several consumers
      (app shell, Google Maps loader), so share a single request. */
  getConfig(): Observable<Config> {
    if (!this.config$) {
      this.config$ = this.http.get<Config>(`${this.base}/config`).pipe(shareReplay(1));
    }
    return this.config$;
  }

  getBackendVersion(): Observable<{ version: string }> {
    return this.http.get<{ version: string }>(`${this.base}/version`);
  }

  listDirectory(query: DirectoryQuery): Observable<DirectoryResponse> {
    return this.http.post<DirectoryResponse>(`${this.base}/files/directory`, query);
  }

  getFileDetails(path: string): Observable<FileInfo> {
    return this.http.get<FileInfo>(`${this.base}/files/details`, { params: { path } });
  }

  getFileExif(path: string): Observable<ExifTag[]> {
    return this.http.get<ExifTag[]>(`${this.base}/files/exif`, { params: { path } });
  }

  scanDirectory(path: string): Observable<string> {
    return this.http.post<string>(`${this.base}/tracking/scan-directory`, { path, generate_clip_preview: true });
  }

  trackFile(path: string): Observable<string> {
    return this.http.post<string>(`${this.base}/tracking/scan-file`, { path, generate_clip_preview: true });
  }

  clipPreviewUrl(md5Hash: string): string {
    return `${this.base}/files/clip-preview/${md5Hash}`;
  }

  fetchFullImage(md5Hash: string): Observable<Blob> {
    return this.http.get(`${this.base}/files/full-image/${md5Hash}`, { responseType: 'blob' });
  }

  renameFile(path: string, newName: string): Observable<FileInfo> {
    return this.http.patch<FileInfo>(`${this.base}/files/rename`, { path, new_name: newName });
  }

  /** Same endpoint as renameFile, but typed for callers (the browser grid) that
      also rename directories and need the `is_directory` flag. */
  renamePath(path: string, newName: string): Observable<RenameResponse> {
    return this.http.patch<RenameResponse>(`${this.base}/files/rename`, { path, new_name: newName });
  }

  previewMove(paths: string[], targetDirectory: string): Observable<MovePreviewResponse> {
    return this.http.post<MovePreviewResponse>(`${this.base}/files/move/preview`, { paths, target_directory: targetDirectory });
  }

  moveFiles(paths: string[], targetDirectory: string): Observable<MoveItemResult[]> {
    return this.http.post<MoveItemResult[]>(`${this.base}/files/move`, { paths, target_directory: targetDirectory });
  }

  mkdir(parent: string, name: string): Observable<MkdirResponse> {
    return this.http.post<MkdirResponse>(`${this.base}/files/mkdir`, { parent, name });
  }

  getTasks(): Observable<Task[]> {
    return this.http.get<Task[]>(`${this.base}/tasks/`);
  }

  deleteTask(id: string): Observable<Task> {
    return this.http.delete<Task>(`${this.base}/tasks/${id}`);
  }

  /** Removes all COMPLETED tasks in one call (backend: DELETE /tasks/completed). */
  clearCompletedTasks(): Observable<Task[]> {
    return this.http.delete<Task[]>(`${this.base}/tasks/completed`);
  }

  getAllKeywords(): Observable<string[]> {
    return this.http.get<string[]>(`${this.base}/keywords/`);
  }

  addKeyword(md5Hash: string, keyword: string): Observable<void> {
    return this.http.post<void>(`${this.base}/keywords/`, { md5_hash: md5Hash, keyword });
  }

  removeKeyword(md5Hash: string, keyword: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/keywords/`, { body: { md5_hash: md5Hash, keyword } });
  }

  getFacetValues(field: string, q: string): Observable<string[]> {
    return this.http.get<string[]>(`${this.base}/files/search-facets`, { params: { field, q } });
  }

  searchFiles(query: FileSearchQuery): Observable<SearchResponse> {
    return this.http.post<SearchResponse>(`${this.base}/files/search`, query);
  }

  getMapPoints(bounds: { west: number; south: number; east: number; north: number }, zoom: number): Observable<MapPoint[]> {
    return this.http.get<MapPoint[]>(`${this.base}/locations/map-points`, {
      params: {
        bbox_west:  bounds.west,
        bbox_south: bounds.south,
        bbox_east:  bounds.east,
        bbox_north: bounds.north,
        zoom,
      }
    });
  }

  getLocations(): Observable<Location[]> {
    return this.http.get<Location[]>(`${this.base}/locations/`);
  }

  createLocation(data: Partial<Location>): Observable<Location> {
    return this.http.post<Location>(`${this.base}/locations/`, data);
  }

  assignLocation(md5Hash: string, locationId: number | null): Observable<FileInfo> {
    return this.http.patch<FileInfo>(`${this.base}/files/location`, { md5_hash: md5Hash, location_id: locationId });
  }

  classifyShot(path: string): Observable<ShotClassification> {
    return this.http.post<ShotClassification>(`${this.base}/ai/classify-shot`, { path });
  }

  // ── Lists ──

  getLists(): Observable<FileList[]> {
    return this.http.get<FileList[]>(`${this.base}/lists`);
  }

  createList(name: string): Observable<FileList> {
    return this.http.post<FileList>(`${this.base}/lists`, { name });
  }

  renameList(id: number, name: string): Observable<FileList> {
    return this.http.patch<FileList>(`${this.base}/lists/${id}`, { name });
  }

  deleteList(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/lists/${id}`);
  }

  getListItems(id: number, page: number, pageSize: number): Observable<ListItemsResponse> {
    return this.http.get<ListItemsResponse>(`${this.base}/lists/${id}/items`, {
      params: { page, page_size: pageSize }
    });
  }

  addFilesToList(id: number, md5Hashes: string[]): Observable<AddFilesToListResponse> {
    return this.http.post<AddFilesToListResponse>(`${this.base}/lists/${id}/items`, { md5_hashes: md5Hashes });
  }

  removeFileFromList(id: number, md5Hash: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/lists/${id}/items/${md5Hash}`);
  }

  getListItemByCode(id: number, code: string): Observable<ListItem> {
    return this.http.get<ListItem>(`${this.base}/lists/${id}/items/by-code/${code}`);
  }

  listExportPdfUrl(id: number): string {
    return `${this.base}/lists/${id}/export.pdf`;
  }

  // ── Maintenance / troubleshooting ──

  getMissingFiles(path?: string): Observable<MissingFile[]> {
    return this.http.get<MissingFile[]>(`${this.base}/trouble-shooting/missing-files`, {
      params: path ? { path } : {}
    });
  }

  rediscover(path: string, trackNew: boolean): Observable<string> {
    return this.http.post<string>(`${this.base}/tracking/rediscover`, {
      path, track_new: trackNew, generate_clip_preview: true,
    });
  }

  // ── Path conflicts (#25) ──

  getConflicts(): Observable<ConflictEntry[]> {
    return this.http.get<ConflictEntry[]>(`${this.base}/tracking/conflicts`);
  }

  getConflictsCount(): Observable<{ count: number }> {
    return this.http.get<{ count: number }>(`${this.base}/tracking/conflicts/count`);
  }

  resolveConflict(md5Hash: string, chosenPath: string): Observable<void> {
    return this.http.post<void>(`${this.base}/tracking/conflicts/resolve`, {
      md5_hash: md5Hash, chosen_path: chosenPath,
    });
  }

  resolveConflictsBatch(strategy: ResolveBatchStrategy, md5Hashes: string[]): Observable<ResolveBatchResponse> {
    return this.http.post<ResolveBatchResponse>(`${this.base}/tracking/conflicts/resolve-batch`, {
      strategy, md5_hashes: md5Hashes,
    });
  }
}
