import { DatePipe } from '@angular/common';
import { Component, computed, ElementRef, HostListener, inject, OnInit, signal, ViewChild } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { switchMap, map, tap } from 'rxjs';

import { ContextMenuComponent } from './context-menu/context-menu.component';
import { ApiService } from '../services/api.service';
import { FileInfo, Location, PathChild, VIDEO_TYPES, PHOTO_TYPES } from '../models';

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

  editingName = signal(false);
  editNameValue = signal('');
  renameError = signal<string | null>(null);
  newKeywordValue = signal('');
  allKeywords = signal<string[]>([]);
  allLocations = signal<Location[]>([]);
  showCreateLocation = signal(false);
  newLocCountry = signal('');
  newLocRegion  = signal('');
  newLocCity    = signal('');
  newLocName    = signal('');
  newLocLat     = signal('');
  newLocLon     = signal('');
  keywordSuggestions = computed(() => {
    const input = this.newKeywordValue().toLowerCase();
    const applied = new Set(this.selectedFile()?.keywords ?? []);
    return this.allKeywords().filter(
      kw => !applied.has(kw) && (input === '' || kw.toLowerCase().includes(input))
    );
  });
  @ViewChild('nameInput') nameInputRef?: ElementRef<HTMLInputElement>;

  dirs        = computed(() => this.entries().filter(e => e.type === 'directory'));
  videoFiles  = computed(() => this.entries().filter(e => e.type === 'file' && VIDEO_TYPES.includes(e.media_type as any)));
  photoFiles  = computed(() => this.entries().filter(e => e.type === 'file' && PHOTO_TYPES.includes(e.media_type as any)));
  untrackedFiles  = computed(() => this.entries().filter(
    e => e.type === 'file' && !VIDEO_TYPES.includes(e.media_type as any) && !PHOTO_TYPES.includes(e.media_type as any)
  ));
  hasMore = computed(() => this.entries().length < this.total());
  showDetail = computed(() => this.loadingDetails() || !!this.selectedFile());

  previewUrl = computed(() => {
    const file = this.selectedFile();
    if (!file?.md5_hash || !VIDEO_TYPES.includes(file.media_type as any)) return null;
    return this.api.clipPreviewUrl(file.md5_hash);
  });

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
          this.api.getAllKeywords().subscribe({ next: kws => this.allKeywords.set(kws) });
          this.api.getLocations().subscribe({ next: locs => this.allLocations.set(locs) });
        },
        error: () => this.loadingDetails.set(false),
      });
    }
  }

  @HostListener('document:keydown.escape')
  onEscapeKey() {
    if (this.showDetail()) this.closeDetails();
  }

  closeDetails() {
    this.selectedFile.set(null);
    this.loadingDetails.set(false);
    this.editingName.set(false);
    this.renameError.set(null);
    this.showCreateLocation.set(false);
    this.newLocCountry.set('');
    this.newLocRegion.set('');
    this.newLocCity.set('');
    this.newLocName.set('');
    this.newLocLat.set('');
    this.newLocLon.set('');
  }

  startEditName() {
    const file = this.selectedFile();
    if (!file) return;
    this.editNameValue.set(file.name);
    this.editingName.set(true);
    this.renameError.set(null);
    setTimeout(() => {
      const el = this.nameInputRef?.nativeElement;
      if (el) {
        el.focus();
        const lastDot = file.name.lastIndexOf('.');
        const stemEnd = lastDot > 0 ? lastDot : file.name.length;
        el.setSelectionRange(0, stemEnd);
      }
    }, 0);
  }

  saveNameEdit() {
    const file = this.selectedFile();
    if (!file) return;
    const newName = this.editNameValue().trim();
    if (!newName || newName === file.name) {
      this.cancelNameEdit();
      return;
    }
    this.api.renameFile(file.path, newName).subscribe({
      next: (updated) => {
        this.selectedFile.set(updated);
        this.entries.update(list =>
          list.map(e => e.path === file.path ? { ...e, name: updated.name, path: updated.path } : e)
        );
        this.editingName.set(false);
        this.renameError.set(null);
      },
      error: (err) => this.renameError.set(err.error?.detail ?? 'Rename failed'),
    });
  }

  cancelNameEdit() {
    this.editingName.set(false);
    this.renameError.set(null);
  }

  private reloadFileDetails(path: string) {
    this.api.getFileDetails(path).subscribe({
      next: info => this.selectedFile.set(info),
    });
  }

  addKeyword() {
    const file = this.selectedFile();
    const kw = this.newKeywordValue().trim();
    if (!file?.md5_hash || !kw) return;
    this.newKeywordValue.set('');
    this.api.addKeyword(file.md5_hash, kw).subscribe({
      next: () => {
        this.reloadFileDetails(file.path);
        this.api.getAllKeywords().subscribe({ next: kws => this.allKeywords.set(kws) });
      },
    });
  }

  removeKeyword(keyword: string) {
    const file = this.selectedFile();
    if (!file?.md5_hash) return;
    this.api.removeKeyword(file.md5_hash, keyword).subscribe({
      next: () => this.reloadFileDetails(file.path),
    });
  }

  assignLocation(locationId: number | null) {
    const file = this.selectedFile();
    if (!file?.md5_hash) return;
    this.api.assignLocation(file.md5_hash, locationId).subscribe({
      next: (updated) => this.selectedFile.set(updated),
    });
  }

  createLocation() {
    const lat = this.newLocLat() ? parseFloat(this.newLocLat()) : null;
    const lon = this.newLocLon() ? parseFloat(this.newLocLon()) : null;
    this.api.createLocation({
      country: this.newLocCountry() || null,
      region:  this.newLocRegion()  || null,
      city:    this.newLocCity()    || null,
      name:    this.newLocName()    || null,
      latitude:  isNaN(lat as any) ? null : lat,
      longitude: isNaN(lon as any) ? null : lon,
    }).subscribe({
      next: (loc) => {
        this.allLocations.update(list => [...list, loc]);
        this.assignLocation(loc.id);
        this.cancelCreateLocation();
      },
    });
  }

  cancelCreateLocation() {
    this.showCreateLocation.set(false);
    this.newLocCountry.set('');
    this.newLocRegion.set('');
    this.newLocCity.set('');
    this.newLocName.set('');
    this.newLocLat.set('');
    this.newLocLon.set('');
  }

  formatLocation(loc: Location): string {
    return [loc.country, loc.region, loc.city, loc.name].filter(Boolean).join(' › ');
  }

  onBackgroundContextMenu(event: MouseEvent) {
    const path = this.currentPath();
    if (!path) return;
    const syntheticDir: PathChild = { name: path.split('/').pop() || path, path, type: 'directory', file_extension: null, tracked: null };
    this.onEntryContextMenu(event, syntheticDir);
  }

  onEntryContextMenu(event: MouseEvent, entry: PathChild) {
    event.preventDefault();
    event.stopPropagation();
    this.contextMenuX.set(event.clientX);
    this.contextMenuY.set(event.clientY);
    this.contextMenuEntry.set(entry);
  }

  closeContextMenu() {
    this.contextMenuEntry.set(null);
  }

  scanCurrentDirectory() {
    const path = this.currentPath();
    if (!path) return;
    this.api.scanDirectory(path).subscribe({ next: () => this.api.taskRefresh$.next() });
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

  entryPreviewUrl(entry: PathChild): string | null {
    if (!entry.md5_hash || !VIDEO_TYPES.includes(entry.media_type as any)) return null;
    return this.api.clipPreviewUrl(entry.md5_hash);
  }
}
