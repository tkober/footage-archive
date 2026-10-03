import { Component, computed, ElementRef, HostListener, inject, OnInit, signal, ViewChild } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { forkJoin, switchMap, map, tap } from 'rxjs';

import { ContextMenuComponent, ContextMenuActionEvent } from './context-menu/context-menu.component';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { ListPickerComponent } from '../shared/list-picker/list-picker.component';
import { FolderPickerComponent } from '../shared/folder-picker/folder-picker.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { RediscoverDialogComponent } from '../shared/rediscover-dialog/rediscover-dialog.component';
import { ComparisonComponent } from '../comparison/comparison.component';
import { ApiService } from '../services/api.service';
import { FileInfo, FileList, Location, MoveItemResult, MovePreviewResponse, PathChild, RenameResponse, VIDEO_TYPES, PHOTO_TYPES } from '../models';

const PAGE_SIZE = 50;
const BULK_RESULT_TIMEOUT_MS = 4000;
const FILE_OP_RESULT_TIMEOUT_MS = 6000;

/** Pending rename awaiting confirmation (directory rename, or a file rename whose sidecars move along). */
interface PendingRename {
  entry: PathChild;
  newName: string;
  preview: MovePreviewResponse;
}

/** Pending move awaiting confirmation, from either the context menu (single entry) or bulk mode. */
interface PendingMove {
  paths: string[];
  targetDirectory: string;
  preview: MovePreviewResponse;
}

@Component({
  selector: 'app-browser',
  standalone: true,
  imports: [ContextMenuComponent, FileDetailPanelComponent, ListPickerComponent, FolderPickerComponent, ConfirmDialogComponent, RediscoverDialogComponent, ComparisonComponent],
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
  bulkListResult = signal<string | null>(null);
  private bulkListResultTimer?: ReturnType<typeof setTimeout>;

  // Comparison view
  showComparison = signal(false);

  // Inline rename (file or directory tile, triggered from the context menu)
  renamingPath  = signal<string | null>(null);
  renameValue   = signal('');
  renameError   = signal<string | null>(null);
  pendingRename = signal<PendingRename | null>(null);
  @ViewChild('renameInputEl') renameInputRef?: ElementRef<HTMLInputElement>;

  // Move to… (folder picker + confirm), shared by the context menu and bulk mode
  movePickerPaths = signal<string[] | null>(null);
  pendingMove     = signal<PendingMove | null>(null);
  moveError       = signal<string | null>(null);

  // Rediscover (context menu on a directory) — a single checkbox confirm,
  // the folder is already known.
  rediscoverPath = signal<string | null>(null);

  // Transient feedback for rename/move results
  fileOpMessage = signal<string | null>(null);
  private fileOpMessageTimer?: ReturnType<typeof setTimeout>;

  dirs           = computed(() => this.entries().filter(e => e.type === 'directory'));
  videoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && VIDEO_TYPES.includes(e.media_type as any)));
  photoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && PHOTO_TYPES.includes(e.media_type as any)));
  untrackedFiles = computed(() => this.entries().filter(
    e => e.type === 'file' && !VIDEO_TYPES.includes(e.media_type as any) && !PHOTO_TYPES.includes(e.media_type as any)
  ));
  hasMore    = computed(() => this.entries().length < this.total());
  bulkTrackedCount = computed(() => this.bulkTrackedEntries().length);
  showDetail = computed(() => this.loadingDetails() || !!this.selectedFile());

  // Detail-panel sibling navigation (photos only — matches the photo viewer).
  photoNavCount = computed(() => this.photoFiles().length);
  photoNavIndex = computed(() => {
    const cur = this.selectedFile();
    if (!cur) return -1;
    return this.photoFiles().findIndex(e => e.path === cur.path);
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

  // Step to the prev/next photo in the directory. Keeps the current panel
  // visible until the new details arrive (avoids a flash); the panel's own
  // file-sync effect resets its HQ/zoom state when the input file changes.
  navigatePhoto(dir: number) {
    const list = this.photoFiles();
    const target = list[this.photoNavIndex() + dir];
    if (!target) return;
    this.api.getFileDetails(target.path).subscribe({
      next: info => this.selectedFile.set(info),
    });
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

  onContextMenuAction(event: ContextMenuActionEvent) {
    const { kind, entry } = event;
    if (kind === 'scan' || kind === 'track') {
      const call = kind === 'scan' ? this.api.scanDirectory(entry.path) : this.api.trackFile(entry.path);
      call.subscribe({ next: () => this.api.taskRefresh$.next() });
    } else if (kind === 'rename') {
      this.startRename(entry);
    } else if (kind === 'move') {
      this.openMovePicker([entry.path]);
    } else if (kind === 'rediscover') {
      this.rediscoverPath.set(entry.path);
    }
  }

  // ── Rediscover (context menu on a directory) ──

  closeRediscover() {
    this.rediscoverPath.set(null);
  }

  onRediscoverStarted() {
    this.rediscoverPath.set(null);
    this.showFileOpMessage('Rediscover started — see tasks.');
  }

  // ── Rename (inline edit on the grid tile) ──

  isRenaming(entry: PathChild): boolean {
    return this.renamingPath() === entry.path;
  }

  startRename(entry: PathChild) {
    this.renamingPath.set(entry.path);
    this.renameValue.set(entry.name);
    this.renameError.set(null);
    setTimeout(() => {
      const el = this.renameInputRef?.nativeElement;
      if (el) {
        el.focus();
        const lastDot = entry.name.lastIndexOf('.');
        const stemEnd = entry.type === 'file' && lastDot > 0 ? lastDot : entry.name.length;
        el.setSelectionRange(0, stemEnd);
      }
    });
  }

  cancelRename() {
    this.renamingPath.set(null);
    this.renameError.set(null);
  }

  /** Enter on the inline rename input: directories always need a confirm (prefix update
      can touch many rows); files only need one when sidecars will move along too. */
  commitRename(entry: PathChild) {
    const newName = this.renameValue().trim();
    if (!newName || newName === entry.name) { this.cancelRename(); return; }

    const parent = this.parentOf(entry.path);
    this.api.previewMove([entry.path], parent).subscribe({
      next: preview => {
        if (entry.type === 'directory' || preview.sidecars.length > 0) {
          this.pendingRename.set({ entry, newName, preview });
        } else {
          this.executeRename(entry, newName);
        }
      },
      // Preview is informational only — if it fails for some reason, still let the
      // rename itself be attempted; the rename call surfaces any real error.
      error: () => this.executeRename(entry, newName),
    });
  }

  confirmPendingRename() {
    const p = this.pendingRename();
    if (!p) return;
    this.pendingRename.set(null);
    this.executeRename(p.entry, p.newName);
  }

  cancelPendingRename() {
    this.pendingRename.set(null);
  }

  renamePreviewMessage(): string {
    const p = this.pendingRename();
    if (!p) return '';
    if (p.entry.type === 'directory') {
      return `Rename folder to "${p.newName}"? ${p.preview.file_count} files `
        + `(${p.preview.tracked_count} tracked) will be renamed along with it.`;
    }
    return `Rename "${p.entry.name}" to "${p.newName}"? ${p.preview.sidecars.length} sidecar file(s) `
      + `will be renamed along with it.`;
  }

  private executeRename(entry: PathChild, newName: string) {
    this.api.renamePath(entry.path, newName).subscribe({
      next: resp => this.applyRenameResult(entry, resp),
      error: err => this.renameError.set(err.error?.detail ?? 'Rename failed'),
    });
  }

  private applyRenameResult(entry: PathChild, resp: RenameResponse) {
    this.renamingPath.set(null);
    this.renameError.set(null);

    this.entries.update(list => list.map(e =>
      e.path === entry.path ? { ...e, name: resp.name, path: resp.path } : e
    ));

    // The renamed directory is the one we're currently looking at (via the
    // background context menu on the current path itself) — follow it.
    if (this.currentPath() === entry.path && resp.path !== entry.path) {
      this.navigateTo(resp.path);
    }

    // The open detail panel is showing the file that was just renamed.
    const sel = this.selectedFile();
    if (sel && sel.path === entry.path) {
      this.selectedFile.set({ ...sel, name: resp.name, path: resp.path });
    }

    this.showFileOpMessage(`Renamed to "${resp.name}"`);
  }

  // ── Move to… (folder picker + confirm) ──

  openMovePicker(paths: string[]) {
    this.moveError.set(null);
    this.movePickerPaths.set(paths);
  }

  closeMovePicker() {
    this.movePickerPaths.set(null);
  }

  onFolderPicked(targetDirectory: string) {
    const paths = this.movePickerPaths();
    this.movePickerPaths.set(null);
    if (!paths) return;
    this.api.previewMove(paths, targetDirectory).subscribe({
      next: preview => this.pendingMove.set({ paths, targetDirectory, preview }),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Could not preview the move'),
    });
  }

  movePreviewMessage(): string {
    const p = this.pendingMove();
    if (!p) return '';
    const sidecarsPart = p.preview.sidecars.length ? ` + ${p.preview.sidecars.length} sidecars` : '';
    return `${p.preview.file_count} files (${p.preview.tracked_count} tracked)${sidecarsPart} `
      + `will be moved to "${this.relativePath(p.targetDirectory)}".`;
  }

  confirmPendingMove() {
    const p = this.pendingMove();
    if (!p) return;
    this.pendingMove.set(null);
    this.api.moveFiles(p.paths, p.targetDirectory).subscribe({
      next: results => this.applyMoveResults(results, p.targetDirectory),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Move failed'),
    });
  }

  cancelPendingMove() {
    this.pendingMove.set(null);
  }

  private applyMoveResults(results: MoveItemResult[], targetDirectory: string) {
    const ok = results.filter(r => r.ok);
    const failed = results.filter(r => !r.ok);

    let message = `${ok.length} moved`;
    if (failed.length) {
      message += ` · ${failed.length} failed: ` + failed.map(f => f.error).join('; ');
    }
    this.showFileOpMessage(message, failed.length > 0);

    // Clear the bulk selection for whatever moved; leftover (failed) selection stays.
    if (ok.length) {
      const movedPaths = new Set(ok.map(r => r.path));
      this.bulkSelected.update(sel => new Set([...sel].filter(p => !movedPaths.has(p))));
    }

    // Did we move the directory we're currently looking at (or an ancestor of it)?
    let navigateTarget: string | null = null;
    for (const r of ok) {
      if (!r.new_path) continue;
      const cur = this.currentPath();
      if (!cur) continue;
      if (cur === r.path) {
        navigateTarget = r.new_path;
      } else if (cur.startsWith(r.path + '/')) {
        navigateTarget = r.new_path + cur.slice(r.path.length);
      }
    }

    // Did we move the file the detail panel is showing?
    const sel = this.selectedFile();
    if (sel) {
      const moved = ok.find(r => r.new_path && (sel.path === r.path || sel.path.startsWith(r.path + '/')));
      if (moved && moved.new_path) {
        const newSelPath = sel.path === moved.path ? moved.new_path : moved.new_path + sel.path.slice(moved.path.length);
        this.api.getFileDetails(newSelPath).subscribe({
          next: info => this.selectedFile.set(info),
          error: () => this.closeDetails(),
        });
      }
    }

    if (navigateTarget) {
      this.navigateTo(navigateTarget);
    } else {
      this.reloadCurrentDirectory();
    }
  }

  private reloadCurrentDirectory() {
    const path = this.currentPath();
    if (path) this.loadDirectory(path);
  }

  private showFileOpMessage(message: string, sticky = false) {
    this.fileOpMessage.set(message);
    if (this.fileOpMessageTimer) clearTimeout(this.fileOpMessageTimer);
    if (!sticky) {
      this.fileOpMessageTimer = setTimeout(() => this.fileOpMessage.set(null), FILE_OP_RESULT_TIMEOUT_MS);
    }
  }

  dismissFileOpMessage() {
    this.fileOpMessage.set(null);
    if (this.fileOpMessageTimer) {
      clearTimeout(this.fileOpMessageTimer);
      this.fileOpMessageTimer = undefined;
    }
  }

  bulkMoveTo() {
    if (!this.bulkSelected().size) return;
    this.openMovePicker([...this.bulkSelected()]);
  }

  private parentOf(path: string): string {
    const idx = path.lastIndexOf('/');
    return idx > 0 ? path.slice(0, idx) : '/';
  }

  relativePath(path: string): string {
    const root = this.rootDir();
    if (!root) return path;
    if (path === root) return '/';
    return path.startsWith(root) ? path.slice(root.length).replace(/^\/+/, '') : path;
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
    this.clearBulkListResult();
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

  bulkAddToList(list: FileList) {
    const targets = this.bulkTrackedEntries();
    const untrackedCount = this.bulkSelected().size - targets.length;
    if (!targets.length) return;
    this.bulkApplying.set(true);
    this.clearBulkListResult();
    const md5s = targets.map(e => e.md5_hash!);
    this.api.addFilesToList(list.id, md5s).subscribe({
      next: resp => {
        this.bulkApplying.set(false);
        const parts: string[] = [];
        if (resp.added.length) parts.push(`Added ${resp.added.length} to '${list.name}'`);
        if (resp.existing.length) parts.push(`${resp.existing.length} already in list`);
        if (untrackedCount > 0) parts.push(`${untrackedCount} untracked skipped`);
        this.showBulkListResult(parts.join(' · ') || `Nothing added to '${list.name}'`);
      },
      error: () => {
        this.bulkApplying.set(false);
        this.showBulkListResult(`Failed to add to '${list.name}'`);
      },
    });
  }

  private showBulkListResult(message: string) {
    this.bulkListResult.set(message);
    if (this.bulkListResultTimer) clearTimeout(this.bulkListResultTimer);
    this.bulkListResultTimer = setTimeout(() => this.bulkListResult.set(null), BULK_RESULT_TIMEOUT_MS);
  }

  private clearBulkListResult() {
    this.bulkListResult.set(null);
    if (this.bulkListResultTimer) {
      clearTimeout(this.bulkListResultTimer);
      this.bulkListResultTimer = undefined;
    }
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
