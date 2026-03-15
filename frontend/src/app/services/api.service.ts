import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject } from 'rxjs';

import { environment } from '../../environments/environment';
import { Config, DirectoryQuery, DirectoryResponse, FileInfo, Task } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly base = environment.apiUrl;
  readonly taskRefresh$ = new Subject<void>();

  constructor(private http: HttpClient) {}

  getConfig(): Observable<Config> {
    return this.http.get<Config>(`${this.base}/config`);
  }

  listDirectory(query: DirectoryQuery): Observable<DirectoryResponse> {
    return this.http.post<DirectoryResponse>(`${this.base}/files/directory`, query);
  }

  getFileDetails(path: string): Observable<FileInfo> {
    return this.http.get<FileInfo>(`${this.base}/files/details`, { params: { path } });
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

  renameFile(path: string, newName: string): Observable<FileInfo> {
    return this.http.patch<FileInfo>(`${this.base}/files/rename`, { path, new_name: newName });
  }

  getTasks(): Observable<Task[]> {
    return this.http.get<Task[]>(`${this.base}/tasks/`);
  }

  deleteTask(id: string): Observable<Task> {
    return this.http.delete<Task>(`${this.base}/tasks/${id}`);
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
}
