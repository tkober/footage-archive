import { Component, computed, HostListener, inject, OnInit, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { forkJoin, switchMap, map, tap } from 'rxjs';

import { ContextMenuComponent } from './context-menu/context-menu.component';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { ComparisonComponent } from '../comparison/comparison.component';
import { ApiService } from '../services/api.service';
import { FileInfo, Location, PathChild, VIDEO_TYPES, PHOTO_TYPES } from '../models';

const PAGE_SIZE = 50;

@Component({
  selector: 'app-browser',
  standalone: true,
  imports: [ContextMenuComponent, FileDetailPanelComponent, ComparisonComponent],
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

  // Bulk mode
  bulkMode       = signal(false);
  bulkSelected   = signal<Set<string>>(new Set());
  bulkKeyword    = signal('');
  bulkLocationId = signal('');
  bulkApplying   = signal(false);
  allKeywords    = signal<string[]>([]);
  allLocations   = signal<Location[]>([]);

  // Comparison view
  showComparison = signal(false);

  dirs           = computed(() => this.entries().filter(e => e.type === 'directory'));
  videoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && VIDEO_TYPES.includes(e.media_type as any)));
  photoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && PHOTO_TYPES.includes(e.media_type as any)));
  untrackedFiles = computed(() => this.entries().filter(
    e => e.type === 'file' && !VIDEO_TYPES.includes(e.media_type as any) && !PHOTO_TYPES.includes(e.media_type as any)
  ));
  hasMore    = computed(() => this.entries().length < this.total());
  showDetail = computed(() => this.loadingDetails() || !!this.selectedFile());

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
    if (this.bulkMode()) {
      if (entry.type === 'file') this.toggleBulkSelect(entry);
      return;
    }
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

  @HostListener('document:keydown.escape')
  onEscapeKey() {
    if (this.showComparison()) this.closeComparison();
    else if (this.showDetail()) this.closeDetails();
    else if (this.bulkMode()) this.exitBulkMode();
  }

  closeDetails() {
    this.selectedFile.set(null);
    this.loadingDetails.set(false);
  }

  onFileRenamed(updated: FileInfo) {
    this.selectedFile.set(updated);
    this.entries.update(list =>
      list.map(e => e.md5_hash === updated.md5_hash
        ? { ...e, name: updated.name, path: updated.path }
        : e)
    );
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

  enterBulkMode() {
    this.bulkMode.set(true);
    if (!this.allLocations().length)
      this.api.getLocations().subscribe({ next: locs => this.allLocations.set(locs) });
    if (!this.allKeywords().length)
      this.api.getAllKeywords().subscribe({ next: kws => this.allKeywords.set(kws) });
  }

  exitBulkMode() {
    this.bulkMode.set(false);
    this.bulkSelected.set(new Set());
    this.bulkKeyword.set('');
    this.bulkLocationId.set('');
  }

  toggleBulkSelect(entry: PathChild) {
    this.bulkSelected.update(s => {
      const next = new Set(s);
      next.has(entry.path) ? next.delete(entry.path) : next.add(entry.path);
      return next;
    });
  }

  isBulkSelected(entry: PathChild): boolean {
    return this.bulkSelected().has(entry.path);
  }

  clearBulkSelection() {
    this.bulkSelected.set(new Set());
  }

  selectAllBulk() {
    const all = [...this.videoFiles(), ...this.photoFiles(), ...this.untrackedFiles()]
      .map(e => e.path);
    this.bulkSelected.set(new Set(all));
  }

  private bulkTrackedEntries(): PathChild[] {
    const sel = this.bulkSelected();
    return [...this.videoFiles(), ...this.photoFiles(), ...this.untrackedFiles()]
      .filter(e => sel.has(e.path) && !!e.md5_hash);
  }

  // Selected, tracked photos eligible for the comparison view.
  comparablePhotos(): PathChild[] {
    const sel = this.bulkSelected();
    return this.photoFiles().filter(e => sel.has(e.path) && !!e.md5_hash);
  }

  openComparison() {
    if (this.comparablePhotos().length >= 2) this.showComparison.set(true);
  }

  closeComparison() {
    this.showComparison.set(false);
  }

  bulkAddKeyword() {
    const kw = this.bulkKeyword().trim();
    const targets = this.bulkTrackedEntries();
    if (!kw || !targets.length) return;
    this.bulkApplying.set(true);
    this.bulkKeyword.set('');
    forkJoin(targets.map(e => this.api.addKeyword(e.md5_hash!, kw))).subscribe({
      next: () => this.bulkApplying.set(false),
      error: () => this.bulkApplying.set(false),
    });
  }

  bulkAssignLocation(locationIdOverride?: number) {
    const locationId = locationIdOverride ?? (+this.bulkLocationId() || null);
    if (!locationId) return;
    const targets = this.bulkTrackedEntries();
    if (!targets.length) return;
    this.bulkApplying.set(true);
    forkJoin(targets.map(e => this.api.assignLocation(e.md5_hash!, locationId))).subscribe({
      next: () => { this.bulkApplying.set(false); this.bulkLocationId.set(''); },
      error: () => { this.bulkApplying.set(false); this.bulkLocationId.set(''); },
    });
  }

  entryPreviewUrl(entry: PathChild): string | null {
    if (!entry.md5_hash) return null;
    if (!VIDEO_TYPES.includes(entry.media_type as any) && !PHOTO_TYPES.includes(entry.media_type as any)) return null;
    return this.api.clipPreviewUrl(entry.md5_hash);
  }

  formatLocation(loc: Location): string {
    const geo = [loc.country, loc.region, loc.city].filter(Boolean).join(' › ');
    return loc.name ? `${loc.name} — ${geo}` : geo;
  }
}
