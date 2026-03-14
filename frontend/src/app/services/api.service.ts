import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

import { environment } from '../../environments/environment';
import { Config, DirectoryQuery, DirectoryResponse } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly base = environment.apiUrl;

  constructor(private http: HttpClient) {}

  getConfig(): Observable<Config> {
    return this.http.get<Config>(`${this.base}/config`);
  }

  listDirectory(query: DirectoryQuery): Observable<DirectoryResponse> {
    return this.http.post<DirectoryResponse>(`${this.base}/files/directory`, query);
  }
}
