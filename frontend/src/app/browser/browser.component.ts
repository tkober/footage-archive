import { DatePipe } from '@angular/common';
import { Component, computed, inject, OnInit, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { switchMap, map, tap } from 'rxjs';

import { ContextMenuComponent } from './context-menu/context-menu.component';
import { ApiService } from '../services/api.service';
import { FileInfo, PathChild } from '../models';

const PAGE_SIZE = 50;

@Component({
  selector: 'app-browser',
  standalone: true,
  imports: [DatePipe, ContextMenuComponent],
  templateUrl: './browser.component.html',
  styleUrl: './browser.component.css'
})
export class BrowserComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  rootDir = signal<string | null>(null);
  currentPath = signal<string | null>(null);
  entries = signal<PathChild[]>([]);
  total = signal(0);
  loading = signal(false);
  loadingMore = signal(false);
  error = signal<string | null>(null);
  selectedFile = signal<FileInfo | null>(null);
  loadingDetails = signal(false);
  contextMenuEntry = signal<PathChild | null>(null);
  contextMenuX = signal(0);
  contextMenuY = signal(0);
  private page = 1;

  hasMore = computed(() => this.entries().length < this.total());

  breadcrumbs = computed(() => {
    const root = this.rootDir();
    const current = this.currentPath();
    if (!root || !current) return [];

    const rootParts = root.split('/').filter(Boolean);
    const currentParts = current.split('/').filter(Boolean);

    return currentParts.slice(rootParts.length - 1).map((label, i) => ({
      label,
      path: '/' + currentParts.slice(0, rootParts.length - 1 + i + 1).join('/')
    }));
  });

  ngOnInit() {
    this.api.getConfig().pipe(
      tap(config => this.rootDir.set(config.root_dir)),
      switchMap(config =>
        this.route.queryParamMap.pipe(
          map(params => params.get('path') ?? config.root_dir)
        )
      )
    ).subscribe({
      next: path => this.loadDirectory(path),
      error: () => this.error.set('Failed to load configuration')
    });
  }

  navigateTo(path: string) {
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { path },
      queryParamsHandling: 'merge'
    });
  }

  private loadDirectory(path: string) {
    this.loading.set(true);
    this.error.set(null);
    this.currentPath.set(path);
    this.selectedFile.set(null);
    this.page = 1;

    this.api.listDirectory({ path, page: 1, page_size: PAGE_SIZE }).subscribe({
      next: response => {
        this.entries.set(response.items);
        this.total.set(response.total);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load directory');
        this.loading.set(false);
      }
    });
  }

  loadMore() {
    const path = this.currentPath();
    if (!path || this.loadingMore()) return;

    this.loadingMore.set(true);
    this.page++;

    this.api.listDirectory({ path, page: this.page, page_size: PAGE_SIZE }).subscribe({
      next: response => {
        this.entries.update(existing => [...existing, ...response.items]);
        this.total.set(response.total);
        this.loadingMore.set(false);
      },
      error: () => {
        this.page--;
        this.loadingMore.set(false);
      }
    });
  }

  onEntryClick(entry: PathChild) {
    if (entry.type === 'directory') {
      this.navigateTo(entry.path);
    } else {
      this.loadingDetails.set(true);
      this.selectedFile.set(null);
      this.api.getFileDetails(entry.path).subscribe({
        next: info => {
          this.selectedFile.set(info);
          this.loadingDetails.set(false);
        },
        error: () => this.loadingDetails.set(false),
      });
    }
  }

  closeDetails() {
    this.selectedFile.set(null);
  }

  onEntryContextMenu(event: MouseEvent, entry: PathChild) {
    event.preventDefault();
    this.contextMenuX.set(event.clientX);
    this.contextMenuY.set(event.clientY);
    this.contextMenuEntry.set(entry);
  }

  closeContextMenu() {
    this.contextMenuEntry.set(null);
  }

  onContextMenuAction(entry: PathChild) {
    const call = entry.type === 'directory'
      ? this.api.scanDirectory(entry.path)
      : this.api.trackFile(entry.path);

    call.subscribe({ next: () => this.api.taskRefresh$.next() });
  }

  formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }
}
