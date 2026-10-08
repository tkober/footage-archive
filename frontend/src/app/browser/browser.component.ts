import { Component, computed, effect, ElementRef, HostListener, inject, OnInit, signal, ViewChild } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { forkJoin, switchMap, map, tap } from 'rxjs';

import { HeaderService } from '../services/header.service';
import { MenuComponent, MenuItem, MenuPoint } from '../shared/menu/menu.component';
import { PopoverComponent } from '../shared/popover/popover.component';
import { DetailNavItem, FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { ListPickerComponent } from '../shared/list-picker/list-picker.component';
import { FolderPickerComponent } from '../shared/folder-picker/folder-picker.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { RediscoverDialogComponent } from '../shared/rediscover-dialog/rediscover-dialog.component';
import { ComparisonComponent } from '../comparison/comparison.component';
import { IconComponent } from '../shared/icon/icon.component';
import { MediaCardComponent } from '../shared/media-card/media-card.component';
import { LoadMoreFooterComponent } from '../shared/load-more-footer/load-more-footer.component';
import { InfiniteScrollDirective } from '../shared/infinite-scroll/infinite-scroll.directive';
import { ApiService } from '../services/api.service';
import { OpenInService } from '../services/open-in.service';
import { PreviewCacheService } from '../services/preview-cache.service';
import { TaskPollService } from '../services/task-poll.service';
import { ToastService } from '../shared/toast/toast.service';
import { DeleteItemResult, DeletePreviewResponse, DirectoryCounts, DirectoryKind, FileInfo, FileList, Location, MoveItemResult, MovePreviewResponse, PathChild, RenameResponse, VIDEO_TYPES, PHOTO_TYPES, formatDeletePreview, formatDurationTc } from '../models';

const PAGE_SIZE = 50;
const SKELETON_CAP = 12;

/** Single-key shortcuts on a focused card / folder tile → menu item id (#41).
    `Delete`/`Backspace` (macOS) move the focused tile to trash (#61). */
const SHORTCUTS: Record<string, string> = {
  ' ': 'open', F2: 'rename', m: 'move', k: 'keyword', l: 'list', t: 'track', r: 'rescan',
  Delete: 'delete', Backspace: 'delete',
};
const FILE_OP_RESULT_TIMEOUT_MS = 6000;
const THUMB_STORAGE_KEY = 'fa-thumb';
const THUMB_MIN = 140;
const THUMB_MAX = 320;
const THUMB_DEFAULT = 200;

type SegmentFilter = 'all' | DirectoryKind;

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

/** Pending delete-to-trash awaiting confirmation (#61), from the context menu
    (single entry) or bulk mode. */
interface PendingDelete {
  paths: string[];
  preview: DeletePreviewResponse;
}

@Component({
  selector: 'app-browser',
  standalone: true,
  imports: [MenuComponent, PopoverComponent, FileDetailPanelComponent, ListPickerComponent, FolderPickerComponent, ConfirmDialogComponent, RediscoverDialogComponent, ComparisonComponent, IconComponent, MediaCardComponent, LoadMoreFooterComponent, InfiniteScrollDirective],
  templateUrl: './browser.component.html',
  styleUrl: './browser.component.css',
  host: { class: 'page-flush' }
})
export class BrowserComponent implements OnInit {
  readonly PAGE_SIZE = PAGE_SIZE;
  private api = inject(ApiService);
  private toast = inject(ToastService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private header = inject(HeaderService);
  private previewCache = inject(PreviewCacheService);
  private taskPoll = inject(TaskPollService);
  private openIn = inject(OpenInService);

  rootDir = signal<string | null>(null);
  /** `.trash` by default — folder under rootDir delete-to-trash moves into (#61). */
  trashDirName = signal<string | null>(null);
  currentPath = signal<string | null>(null);
  entries = signal<PathChild[]>([]);
  total = signal(0);
  counts = signal<DirectoryCounts | null>(null);
  filter = signal<SegmentFilter>('all');
  /** File type dropdown (#72) — a normalised extension like ".rw2", or null
      for "All types". Reset to null when the kind segment changes or when
      navigating to another directory; persists across a plain reload of
      the same directory/kind (mirrors `filter`). */
  extFilter = signal<string | null>(null);
  thumbSize = signal(this.readStoredThumbSize());
  loading = signal(false);
  loadingMore = signal(false);
  loadMoreError = signal<string | null>(null);
  error = signal<string | null>(null);
  selectedFile = signal<FileInfo | null>(null);
  loadingDetails = signal(false);
  // Context menu (#41): opened by right-click (at the pointer) or the card's
  // "⋯" button (anchored to it). `menuSource` is the card / folder tile it
  // belongs to, used to anchor the keyword/list popovers and to give focus back.
  menuEntry  = signal<PathChild | null>(null);
  menuPoint  = signal<MenuPoint | null>(null);
  menuAnchor = signal<HTMLElement | null>(null);
  private menuSource: HTMLElement | null = null;
  menuItems  = computed(() => {
    const entry = this.menuEntry();
    return entry ? this.menuItemsFor(entry) : [];
  });
  menuHeaderMeta = computed(() => {
    const entry = this.menuEntry();
    return entry ? this.entryTypeLabel(entry) : '';
  });

  // "Add keyword…" / "Add to list…" popover anchored at the card (#41)
  quickPop     = signal<{ kind: 'keyword' | 'list'; entry: PathChild; anchor: HTMLElement } | null>(null);
  quickKeyword = signal('');
  private page = 1;

  // Bulk mode
  bulkMode       = signal(false);
  bulkSelected   = signal<Set<string>>(new Set());
  bulkKeyword    = signal('');
  bulkLocationId = signal('');
  bulkApplying   = signal(false);
  allKeywords    = signal<string[]>([]);
  allLocations   = signal<Location[]>([]);
  /** Last tile clicked in selection mode; Shift-click selects up to here. */
  private selectionAnchor: string | null = null;
  /** Keyword / location / list form above the floating bulk bar (#42). */
  bulkPop        = signal<{ kind: 'keyword' | 'location' | 'list'; anchor: HTMLElement } | null>(null);
  locationFilter = signal('');

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

  // Move to trash (#61), shared by the context menu, the keyboard shortcut and bulk mode
  pendingDelete = signal<PendingDelete | null>(null);

  // Rediscover (context menu on a directory) — a single checkbox confirm,
  // the folder is already known.
  rediscoverPath = signal<string | null>(null);

  dirs           = computed(() => this.filter() === 'all' && this.extFilter() === null ? this.entries().filter(e => e.type === 'directory') : []);
  videoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && VIDEO_TYPES.includes(e.media_type as any)));
  photoFiles     = computed(() => this.entries().filter(e => e.type === 'file' && PHOTO_TYPES.includes(e.media_type as any)));
  untrackedFiles = computed(() => this.entries().filter(
    e => e.type === 'file' && !VIDEO_TYPES.includes(e.media_type as any) && !PHOTO_TYPES.includes(e.media_type as any)
  ));
  hasMore    = computed(() => this.entries().length < this.total());
  nextBatchSize = computed(() => Math.max(0, Math.min(PAGE_SIZE, this.total() - this.entries().length)));

  /** Skeleton tiles (#40) while the next page loads — count = the expected
      next page, capped at 12 for rendering. */
  skeletonCount = computed(() => this.loadingMore() ? Math.min(SKELETON_CAP, this.nextBatchSize()) : 0);

  /** Which grid the skeletons go in: obvious for a `kind`-filtered view;
      for "All" it's the section the last loaded item belongs to — a simple
      stand-in for knowing what the next page will actually contain. */
  skeletonTarget = computed<'video' | 'photo' | 'untracked' | null>(() => {
    if (!this.skeletonCount()) return null;
    const f = this.filter();
    if (f === 'video' || f === 'photo' || f === 'untracked') return f;
    const last = this.entries().at(-1);
    if (!last || last.type !== 'file') return null;
    const kind = this.cardKind(last);
    return kind === 'other' ? 'untracked' : kind;
  });

  skeletons = computed(() => Array.from({ length: this.skeletonCount() }, (_, i) => i));
  bulkTrackedCount = computed(() => this.bulkTrackedEntries().length);
  /** Files in the order they're rendered (videos, stills, untracked): the range for Shift-click. */
  orderedFiles = computed(() => [...this.videoFiles(), ...this.photoFiles(), ...this.untrackedFiles()]);
  filteredLocations = computed(() => {
    const q = this.locationFilter().trim().toLowerCase();
    const locs = this.allLocations();
    return q ? locs.filter(l => this.formatLocation(l).toLowerCase().includes(q)) : locs;
  });
  showDetail = computed(() => this.loadingDetails() || !!this.selectedFile());

  /** Section-heading counts (#39) come from the server-side `counts` for the
      whole directory, not from however many rows happen to be loaded/paged.
      With an extension filter active (#72), `counts.video`/`photo`/`untracked`
      no longer match what's actually loaded (they ignore `extension`), so
      count from the loaded rows instead (see `extSectionCount`). */
  videoCount     = computed(() => this.extFilter() !== null ? this.extSectionCount(this.videoFiles().length) : (this.counts()?.video ?? this.videoFiles().length));
  photoCount     = computed(() => this.extFilter() !== null ? this.extSectionCount(this.photoFiles().length) : (this.counts()?.photo ?? this.photoFiles().length));
  untrackedCount = computed(() => this.extFilter() !== null ? this.extSectionCount(this.untrackedFiles().length) : (this.counts()?.untracked ?? this.untrackedFiles().length));

  /** Section count under an extension filter: exact once everything is
      loaded; while paging, `total` if this section holds every loaded row
      (the usual case — one extension, one kind), else the loaded count. */
  private extSectionCount(loaded: number): number {
    if (!this.hasMore()) return loaded;
    return loaded === this.entries().length ? this.total() : loaded;
  }

  /** Filter segments: All / Videos / Stills / Untracked, hiding any
      zero-count segment except All. */
  segments = computed(() => {
    const c = this.counts();
    const all = (c?.video ?? 0) + (c?.photo ?? 0) + (c?.untracked ?? 0);
    const options: { key: SegmentFilter; label: string; count: number }[] = [
      { key: 'all', label: 'All', count: all },
      { key: 'video', label: 'Videos', count: c?.video ?? 0 },
      { key: 'photo', label: 'Stills', count: c?.photo ?? 0 },
      { key: 'untracked', label: 'Untracked', count: c?.untracked ?? 0 },
    ];
    return options.filter(o => o.key === 'all' || o.count > 0);
  });

  /** File type dropdown options (#72): every extension in the current kind
      view (`counts().extensions`, already scoped by `filter`/`kind` but not
      by `extension` itself), sorted alphabetically, label uppercase without
      the leading dot. */
  extOptions = computed(() => {
    const exts = this.counts()?.extensions ?? {};
    return Object.keys(exts).sort((a, b) => a.localeCompare(b)).map(ext => ({
      ext,
      label: ext.replace(/^\./, '').toUpperCase(),
      count: exts[ext],
    }));
  });

  extTotalCount = computed(() => this.extOptions().reduce((sum, o) => sum + o.count, 0));
  private extSelected = computed(() => this.extOptions().find(o => o.ext === this.extFilter()) ?? null);
  extButtonLabel = computed(() => this.extSelected()?.label ?? (this.extFilter() !== null ? this.extFilter()!.replace(/^\./, '').toUpperCase() : 'All types'));
  extButtonCount = computed(() => this.extFilter() !== null ? (this.extSelected()?.count ?? 0) : this.extTotalCount());

  /** File type menu (#83): an app-menu anchored under the dropdown button,
      instead of a native <select> popup. Item id '' = "All types". */
  extMenuAnchor = signal<HTMLElement | null>(null);
  extMenuItems = computed<MenuItem[]>(() => [
    { id: '', label: 'All types', detail: String(this.extTotalCount()), checked: this.extFilter() === null },
    ...this.extOptions().map((o, i) => ({
      id: o.ext, label: o.label, detail: String(o.count), checked: this.extFilter() === o.ext, separatorBefore: i === 0,
    })),
  ]);

  /** Hidden when there's nothing meaningful to choose from — fewer than 2
      extensions and no filter already active (clearing a filter must stay reachable). */
  showExtDropdown = computed(() => this.extOptions().length >= 2 || this.extFilter() !== null);

  /** Ext badge rule (#39): only when the loaded photo entries actually mix
      formats (e.g. JPG + RW2) — otherwise it's noise. */
  mixedPhotoExt = computed(() => {
    const exts = new Set(this.photoFiles().map(e => e.file_extension?.toLowerCase()).filter(Boolean));
    return exts.size > 1;
  });

  // Detail-panel sibling navigation (photos only — matches the photo viewer).
  /** Detail neighbours (#43): the loaded files of the same kind as the open one. */
  private detailSiblings = computed(() => {
    const cur = this.selectedFile();
    if (!cur) return [];
    if (VIDEO_TYPES.includes(cur.media_type as any)) return this.videoFiles();
    if (PHOTO_TYPES.includes(cur.media_type as any)) return this.photoFiles();
    return this.untrackedFiles();
  });
  detailNavItems = computed<DetailNavItem[]>(() => this.detailSiblings().map(e => ({
    key: e.path,
    label: e.name,
    previewUrl: this.entryPreviewUrl(e),
    video: this.cardKind(e) === 'video',
  })));
  detailNavIndex = computed(() => {
    const cur = this.selectedFile();
    return cur ? this.detailSiblings().findIndex(e => e.path === cur.path) : -1;
  });
  currentFolderName = computed(() => this.breadcrumbs().at(-1)?.label ?? null);

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

  /** Publishes this page's breadcrumbs into the shell's topbar (#38) instead
      of rendering its own `<nav>` — Scan/Select stay local (see template),
      only the path trail moves. */
  private publishCrumbs = effect(() => {
    this.header.setCrumbs(this.breadcrumbs().map(c => ({
      label: c.label,
      action: () => this.navigateTo(c.path),
    })));
  });

  ngOnInit() {
    this.api.getConfig().pipe(
      tap(config => { this.rootDir.set(config.root_dir); this.trashDirName.set(config.trash_dir_name); }),
      switchMap(config =>
        this.route.queryParamMap.pipe(
          map(params => params.get('path') ?? config.root_dir)
        )
      )
    ).subscribe({
      next: path => { this.extFilter.set(null); this.loadDirectory(path); },
      error: () => this.error.set('Failed to load configuration')
    });

    // A scan/track/rediscover task completing elsewhere (e.g. the tasks
    // widget) can change which files are tracked — reload so the untracked
    // badge (#134) stays accurate. Same reload as any other refresh here:
    // page resets to 1, selection closes.
    this.api.taskCompleted$.subscribe(() => this.reloadCurrentDirectory());
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
    this.loadMoreError.set(null);
    this.currentPath.set(path);
    this.selectedFile.set(null);
    this.page = 1;

    const f = this.filter(); const kind: DirectoryKind | undefined = f === 'all' ? undefined : f;
    const extension = this.extFilter() ?? undefined;
    this.api.listDirectory({ path, page: 1, page_size: PAGE_SIZE, kind, extension }).subscribe({
      next: response => {
        this.entries.set(response.items);
        this.total.set(response.total);
        this.counts.set(response.counts);
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
    // Guards against a duplicate fetch of the same page: already in
    // flight, or paused on a load-more error until the user retries.
    if (!path || this.loadingMore() || this.loadMoreError() || !this.hasMore()) return;

    this.loadingMore.set(true);
    this.page++;

    const f = this.filter(); const kind: DirectoryKind | undefined = f === 'all' ? undefined : f;
    const extension = this.extFilter() ?? undefined;
    this.api.listDirectory({ path, page: this.page, page_size: PAGE_SIZE, kind, extension }).subscribe({
      next: response => {
        this.entries.update(existing => [...existing, ...response.items]);
        this.total.set(response.total);
        this.counts.set(response.counts);
        this.loadingMore.set(false);
      },
      error: () => {
        this.page--;
        this.loadingMore.set(false);
        this.loadMoreError.set('Failed to load more.');
      }
    });
  }

  retryLoadMore() {
    this.loadMoreError.set(null);
    this.loadMore();
  }

  /** Segmented filter (#39): reloads the directory listing scoped to `kind`.
      Resets the extension filter (#72) — the dropdown's options are scoped
      to the selected kind, so a stale extension could silently filter out
      everything once the kind no longer offers it. */
  setFilter(key: SegmentFilter) {
    if (this.filter() === key) return;
    this.filter.set(key);
    this.extFilter.set(null);
    const path = this.currentPath();
    if (path) this.loadDirectory(path);
  }

  openExtMenu(anchor: HTMLElement) {
    this.extMenuAnchor.set(anchor);
  }

  onExtMenuSelect(id: string) {
    this.extMenuAnchor.set(null);
    this.setExtFilter(id || null);
  }

  /** File type dropdown (#72): reloads the directory listing scoped to
      `extension`, same pattern as `setFilter`. */
  setExtFilter(ext: string | null) {
    if (this.extFilter() === ext) return;
    this.extFilter.set(ext);
    const path = this.currentPath();
    if (path) this.loadDirectory(path);
  }

  private readStoredThumbSize(): number {
    try {
      const raw = localStorage.getItem(THUMB_STORAGE_KEY);
      const n = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(n)) return Math.min(THUMB_MAX, Math.max(THUMB_MIN, n));
    } catch { /* localStorage unavailable — fall back to default */ }
    return THUMB_DEFAULT;
  }

  setThumbSize(value: number) {
    this.thumbSize.set(value);
    try { localStorage.setItem(THUMB_STORAGE_KEY, String(value)); } catch { /* ignore */ }
  }

  formatDuration(tc: string | null | undefined): string | null {
    return formatDurationTc(tc);
  }

/** One small status dot next to the folder name (#139 — replaces the #134
      badge pills, too heavy for a tile per the design revision): a folder's
      direct untracked count AND everything below it, folded into a single
      colour, priority order —
      1. warning (filled `--warning`): untracked here, or below (known and > 0).
      2. neutral (hollow ring, `--faint` — colour-blind-safe, reads as
         "unknown" without relying on hue): below this folder isn't known
         yet (`subtree_status` 'unknown'/'partial') and nothing untracked
         is known either.
      3. success (filled `--ok`): everything known and zero, and the
         subtree actually has media.
      4. no dot: no media at all, nothing unknown below.
      `statusDotLabel` below builds the tooltip/aria-label's text for
      whichever of these this resolves to; `null` here means "no dot", so
      callers never render one. */
  folderStatusDot(dir: PathChild): 'warn' | 'neutral' | 'ok' | null {
    const ownUntracked = dir.untracked_file_count ?? 0;
    const belowKnown = dir.below_untracked_count != null;
    const below = dir.below_untracked_count ?? 0;

    if (ownUntracked > 0 || (belowKnown && below > 0)) return 'warn';
    if (dir.subtree_status === 'unknown' || dir.subtree_status === 'partial') return 'neutral';

    const hasMedia = (dir.media_file_count ?? 0) > 0 || (dir.subtree_media_count ?? 0) > 0;
    const allKnownAndZero = dir.media_file_count != null && belowKnown && below === 0
      && dir.subtree_status === 'complete';
    return allKnownAndZero && hasMedia ? 'ok' : null;
  }

  /** Full sentence for the dot's `title`/`aria-label` (hover tooltip — and,
      since tooltips don't work on touch, folded into the context menu
      header's subtitle too, see `entryTypeLabel`/`statusMenuSuffix`
      below). E.g. "1 untracked here · 68 below · as of 8 Oct, 09:12",
      "All media files tracked · as of …", "Below this folder: not checked
      yet · 3 tracked here". `null` when `folderStatusDot` is `null` (no
      dot — nothing to say). */
  statusDotLabel(dir: PathChild): string | null {
    const dot = this.folderStatusDot(dir);
    if (!dot) return null;
    const ownUntracked = dir.untracked_file_count ?? 0;
    const below = dir.below_untracked_count ?? 0;
    const belowKnown = dir.below_untracked_count != null;

    const parts: string[] = [];
    if (dot === 'warn') {
      if (ownUntracked > 0) parts.push(`${ownUntracked} untracked here`);
      if (belowKnown && below > 0) parts.push(`${below} below`);
    } else if (dot === 'neutral') {
      parts.push('Below this folder: not checked yet');
      if (dir.media_file_count != null && dir.media_file_count > 0 && ownUntracked === 0) {
        parts.push(`${dir.media_file_count} tracked here`);
      }
    } else {
      parts.push('All media files tracked');
    }
    const walked = this.statusWalkedSuffix(dir);
    if (walked) parts.push(walked);
    return parts.join(' · ');
  }

  /** "as of 8 Oct, 09:12" once this folder's own DirectoryStats row is
      known (`status_walked_at`), null otherwise. */
  private statusWalkedSuffix(dir: PathChild): string | null {
    if (!dir.status_walked_at) return null;
    const d = new Date(dir.status_walked_at);
    const date = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return `as of ${date}, ${time}`;
  }

  /** Short status fragment appended to the folder context-menu header's
      subtitle (#139) — tooltips don't work on touch, so this is the mobile
      equivalent of the dot's title/aria-label, just compact: "" (no dot),
      " · 1 untracked · 68 below" (warn), " · below: unknown" (neutral),
      " · complete" (ok). Used by `entryTypeLabel`. */
  private statusMenuSuffix(dir: PathChild): string {
    const dot = this.folderStatusDot(dir);
    if (!dot) return '';
    if (dot === 'ok') return ' · complete';
    if (dot === 'neutral') return ' · below: unknown';
    const ownUntracked = dir.untracked_file_count ?? 0;
    const below = dir.below_untracked_count ?? 0;
    const belowKnown = dir.below_untracked_count != null;
    const parts: string[] = [];
    if (ownUntracked > 0) parts.push(`${ownUntracked} untracked`);
    if (belowKnown && below > 0) parts.push(`${below} below`);
    return parts.length ? ' · ' + parts.join(' · ') : '';
  }

  onCardMore(entry: PathChild, anchor: HTMLElement) {
    this.openMenu(entry, { anchor, source: anchor.closest('app-media-card') as HTMLElement | null });
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

  @HostListener('document:keydown.escape', ['$event'])
  onEscapeKey(event: Event) {
    // A menu/popover handles its own Esc (and marks the event handled).
    if (event.defaultPrevented || this.menuEntry() || this.quickPop()) return;
    if (this.showComparison()) this.closeComparison();
    else if (this.showDetail()) this.closeDetails();
    else if (this.bulkMode()) this.exitBulkMode();
  }

  closeDetails() {
    this.selectedFile.set(null);
    this.loadingDetails.set(false);
  }

  // Step to a neighbour of the open file (same kind). Keeps the current panel
  // visible until the new details arrive (avoids a flash); the panel's own
  // file-sync effect resets its HQ/zoom state when the input file changes.
  navigateDetail(dir: number) {
    this.jumpDetail(this.detailNavIndex() + dir);
  }

  jumpDetail(index: number) {
    const target = this.detailSiblings()[index];
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
    const source = (event.target as Element | null)?.closest('app-media-card, .folder') as HTMLElement | null;
    this.openMenu(entry, { point: { x: event.clientX, y: event.clientY }, source });
  }

  private openMenu(entry: PathChild, opts: { point?: MenuPoint; anchor?: HTMLElement; source?: HTMLElement | null }) {
    this.quickPop.set(null);
    this.menuSource = opts.source ?? opts.anchor ?? null;
    this.menuPoint.set(opts.point ?? null);
    this.menuAnchor.set(opts.anchor ?? null);
    this.menuEntry.set(entry);
  }

  /** Closed without picking (Esc, click outside): give focus back to the card. */
  closeMenu() {
    const hadMenu = !!this.menuEntry();
    this.menuEntry.set(null);
    this.menuAnchor.set(null);
    this.menuPoint.set(null);
    if (hadMenu) this.focusSource(this.menuSource);
  }

  onMenuSelect(id: string) {
    const entry = this.menuEntry();
    const source = this.menuSource;
    this.menuEntry.set(null);
    if (entry) this.runEntryAction(id, entry, source);
  }

  /** Menu entries for a file or folder. The same ids drive the keyboard
      shortcuts on a focused card (see `onShortcut`). */
  menuItemsFor(entry: PathChild): MenuItem[] {
    if (entry.type === 'directory') {
      const isCurrent = entry.path === this.currentPath();
      return [
        ...(isCurrent ? [] : [{ id: 'open', label: 'Open', icon: 'folder', shortcut: 'Enter' }]),
        { id: 'scan', label: 'Scan folder', icon: 'scan', separatorBefore: !isCurrent },
        { id: 'scan-force', label: 'Scan folder (force rehash)', icon: 'scan' },
        { id: 'scan-untracked', label: 'Scan untracked only', icon: 'scan' },
        { id: 'refresh-status', label: 'Refresh status', icon: 'scan' },
        { id: 'rediscover', label: 'Rediscover…', icon: 'rediscover' },
        { id: 'rename', label: 'Rename', icon: 'edit', shortcut: 'F2', separatorBefore: true },
        { id: 'move', label: 'Move to…', icon: 'move', shortcut: 'M' },
        { id: 'copy', label: 'Copy path', icon: 'copy' },
        { id: 'delete', label: 'Move to trash', icon: 'trash', danger: true, separatorBefore: true, shortcut: 'Delete' },
      ];
    }
    const tracked = entry.tracked === true && !!entry.md5_hash;
    return [
      { id: 'open', label: 'Open', icon: 'eye', shortcut: 'Space' },
      ...this.openIn.appsFor(entry.file_extension).map((app, i) => ({
        id: `open-in:${app.id}`, label: `Open in ${app.label}`, icon: app.icon, shortcut: i === 0 ? 'P' : undefined,
      })),
      ...(tracked
        ? [{ id: 'rescan', label: 'Rescan', icon: 'scan', shortcut: 'R' }]
        : [{ id: 'track', label: 'Track file', icon: 'plus', shortcut: 'T' }]),
      { id: 'keyword', label: 'Add keyword…', icon: 'tag', shortcut: 'K', separatorBefore: true, disabled: !tracked },
      { id: 'list', label: 'Add to list…', icon: 'list', shortcut: 'L', disabled: !tracked },
      { id: 'rename', label: 'Rename', icon: 'edit', shortcut: 'F2', separatorBefore: true },
      { id: 'move', label: 'Move to…', icon: 'move', shortcut: 'M' },
      { id: 'copy', label: 'Copy path', icon: 'copy' },
      { id: 'delete', label: 'Move to trash', icon: 'trash', danger: true, separatorBefore: true, shortcut: 'Delete' },
    ];
  }

  /** "Still · JPG", "Video · MOV · 00:12", "Not tracked yet", "Folder · 18 files". */
  entryTypeLabel(entry: PathChild): string {
    if (entry.type === 'directory') {
      const base = entry.file_count != null ? `Folder · ${entry.file_count} file${entry.file_count === 1 ? '' : 's'}` : 'Folder';
      return base + this.statusMenuSuffix(entry);
    }
    if (entry.tracked !== true) return 'Not tracked yet';
    const ext = (entry.file_extension ?? '').replace(/^\./, '').toUpperCase();
    if (this.cardKind(entry) === 'video') {
      return ['Video', ext, this.formatDuration(entry.duration_tc)].filter(Boolean).join(' · ');
    }
    return ['Still', ext].filter(Boolean).join(' · ');
  }

  private runEntryAction(id: string, entry: PathChild, source: HTMLElement | null) {
    if (id.startsWith('open-in:')) { this.openInApp(id.slice('open-in:'.length), entry); return; }
    switch (id) {
      case 'open':
        this.openEntry(entry);
        break;
      case 'scan':
        this.api.scanDirectory(entry.path).subscribe({
          next: () => { this.api.taskRefresh$.next(); this.toast.show(`Scan started for ${entry.name}`); },
        });
        break;
      case 'scan-force':
        this.api.scanDirectory(entry.path, { forceRehash: true }).subscribe({
          next: () => { this.api.taskRefresh$.next(); this.toast.show(`Force-rehash scan started for ${entry.name}`); },
        });
        break;
      case 'scan-untracked':
        this.api.scanDirectory(entry.path, { onlyUntracked: true }).subscribe({
          next: () => { this.api.taskRefresh$.next(); this.toast.show(`Scanning untracked files in ${entry.name}`); },
        });
        break;
      case 'refresh-status':
        this.api.census(entry.path).subscribe({
          next: () => { this.api.taskRefresh$.next(); this.toast.show('Updating folder status — see tasks.'); },
        });
        break;
      case 'track':
        this.api.trackFile(entry.path).subscribe({
          next: () => { this.api.taskRefresh$.next(); this.toast.show(`Tracking ${entry.name}`); },
        });
        break;
      case 'rescan':
        if (entry.md5_hash) this.startRescan([entry.md5_hash]);
        break;
      case 'rediscover':
        this.rediscoverPath.set(entry.path);
        break;
      case 'rename':
        this.startRename(entry);
        break;
      case 'move':
        this.openMovePicker([entry.path]);
        break;
      case 'copy':
        this.copyPath(entry.path);
        this.focusSource(source);
        break;
      case 'delete':
        this.openDeletePreview([entry.path]);
        break;
      case 'keyword':
      case 'list':
        this.openQuickPop(id, entry, source);
        break;
    }
  }

  /** Space / "Open" in the menu: unlike a click, never toggles bulk selection. */
  private openEntry(entry: PathChild) {
    if (entry.type === 'directory') { this.navigateTo(entry.path); return; }
    this.loadingDetails.set(true);
    this.selectedFile.set(null);
    this.api.getFileDetails(entry.path).subscribe({
      next: info => { this.selectedFile.set(info); this.loadingDetails.set(false); },
      error: () => this.loadingDetails.set(false),
    });
  }

  /** "Open in <App>" (#126): hands the file off to the device's Opener (#97/#125). */
  private openInApp(appId: string, entry: PathChild) {
    const app = this.openIn.apps.find(a => a.id === appId);
    if (!app) return;
    if (this.openIn.open(app, entry.path)) {
      this.toast.show(`Opening ${entry.name} in ${app.label}…`);
    } else {
      this.toast.show(`Couldn't open ${entry.name}: path is outside the archive root`);
    }
  }

  // ── Keyboard shortcuts on the focused card / folder tile (#41) ──

  @HostListener('document:keydown', ['$event'])
  onShortcut(ev: Event) {
    const event = ev as KeyboardEvent;
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (this.menuEntry() || this.quickPop() || this.bulkPop() || this.showDetail() || this.showComparison() || this.renamingPath()
        || this.pendingRename() || this.movePickerPaths() || this.pendingMove() || this.pendingDelete() || this.rediscoverPath()) return;
    const target = event.target as HTMLElement | null;
    if (!target || target.closest('input, textarea, select, [contenteditable="true"]')) return;
    const host = target.closest('[data-path]') as HTMLElement | null;
    const entry = host && this.entries().find(e => e.path === host.dataset['path']);
    if (!host || !entry) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const items = this.menuItemsFor(entry);
    // `P` is the shortcut for the (dynamic) first open-in:<appId> item, not a static SHORTCUTS entry.
    const id = key === 'p' ? items.find(i => i.id.startsWith('open-in:'))?.id : SHORTCUTS[key];
    if (!id || !items.some(i => i.id === id && !i.disabled)) return;
    event.preventDefault();
    this.runEntryAction(id, entry, host);
  }

  // ── Quick "Add keyword…" / "Add to list…" popover ──

  private openQuickPop(kind: 'keyword' | 'list', entry: PathChild, source: HTMLElement | null) {
    const host = source ?? (document.querySelector(`[data-path="${CSS.escape(entry.path)}"]`) as HTMLElement | null);
    // app-media-card's host has no box of its own; anchor to the visible tile.
    const anchor = host?.querySelector<HTMLElement>('.card') ?? host;
    if (!anchor) return;
    if (kind === 'keyword' && !this.allKeywords().length) {
      this.api.getAllKeywords().subscribe({ next: kws => this.allKeywords.set(kws) });
    }
    this.quickKeyword.set('');
    this.quickPop.set({ kind, entry, anchor });
    setTimeout(() => document.querySelector<HTMLInputElement>('.quick-form input')?.focus());
  }

  closeQuickPop() {
    const pop = this.quickPop();
    this.quickPop.set(null);
    if (pop) this.focusSource(pop.anchor);
  }

  submitQuickKeyword() {
    const pop = this.quickPop();
    const kw = this.quickKeyword().trim();
    if (!pop || !kw || !pop.entry.md5_hash) return;
    this.api.addKeyword(pop.entry.md5_hash, kw).subscribe({
      next: () => {
        this.toast.show(`Added “${kw}” to ${pop.entry.name}`);
        if (!this.allKeywords().includes(kw)) this.allKeywords.update(list => [...list, kw]);
      },
      error: () => this.toast.show(`Couldn't add “${kw}” to ${pop.entry.name}`),
    });
    this.closeQuickPop();
  }

  quickAddToList(list: FileList) {
    const pop = this.quickPop();
    if (!pop?.entry.md5_hash) return;
    this.api.addFilesToList(list.id, [pop.entry.md5_hash]).subscribe({
      next: resp => this.toast.show(resp.added.length
        ? `Added ${pop.entry.name} to ‘${list.name}’`
        : `${pop.entry.name} is already in ‘${list.name}’`),
      error: () => this.toast.show(`Couldn't add ${pop.entry.name} to ‘${list.name}’`),
    });
    this.closeQuickPop();
  }

  /** navigator.clipboard only exists in secure contexts; the NAS is usually
      reached over plain http, so fall back to a hidden textarea. */
  private copyPath(path: string) {
    const ok = () => this.toast.show('Path copied');
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = path;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const copied = document.execCommand('copy');
      ta.remove();
      copied ? ok() : this.toast.show(`Couldn't copy. Path: ${path}`, { duration: 8000 });
    };
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(path).then(ok, fallback);
    else fallback();
  }

  private focusSource(el: HTMLElement | null) {
    if (!el || !el.isConnected) return;
    (el.matches('[tabindex], button') ? el : el.querySelector<HTMLElement>('[tabindex], button'))?.focus();
  }

  scanCurrentDirectory() {
    const path = this.currentPath();
    if (!path) return;
    this.api.scanDirectory(path).subscribe({ next: () => this.api.taskRefresh$.next() });
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
    this.toast.show(message, { duration: sticky ? 0 : FILE_OP_RESULT_TIMEOUT_MS });
  }

  bulkMoveTo() {
    if (!this.bulkSelected().size) return;
    this.openMovePicker([...this.bulkSelected()]);
  }

  // ── Rescan (#64) ──

  bulkRescan() {
    const targets = this.bulkTrackedEntries();
    if (!targets.length) return;
    const skipped = this.bulkSelected().size - targets.length;
    this.startRescan(targets.map(e => e.md5_hash!), skipped);
  }

  /** Starts POST /tracking/refresh for the given hashes (context menu, bulk
      bar, or the detail panel's own open file) and, once the task finishes,
      cache-busts their preview URL so the grid/detail panel pick up the
      regenerated preview without a page reload. */
  private startRescan(md5Hashes: string[], skipped = 0) {
    if (!md5Hashes.length) return;
    this.api.refreshFiles(md5Hashes).subscribe({
      next: taskId => {
        this.api.taskRefresh$.next();
        this.toast.show(this.withSkipped('Rescan started — see tasks.', skipped));
        this.taskPoll.pollUntilDone(taskId).subscribe(() => {
          this.previewCache.bump(md5Hashes);
          const sel = this.selectedFile();
          if (sel?.md5_hash && md5Hashes.includes(sel.md5_hash)) {
            this.api.getFileDetails(sel.path).subscribe({ next: info => this.selectedFile.set(info) });
          }
        });
      },
      error: () => this.toast.show("Couldn't start the rescan."),
    });
  }

  // ── Move to trash (#61) ──

  bulkDelete() {
    if (!this.bulkSelected().size) return;
    this.openDeletePreview([...this.bulkSelected()]);
  }

  /** Always previews first; a failed preview toasts the error and never opens
      the confirm dialog (unlike rename, there's no fallback-without-confirm). */
  openDeletePreview(paths: string[]) {
    if (!paths.length) return;
    this.api.previewDelete(paths).subscribe({
      next: preview => this.pendingDelete.set({ paths, preview }),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Could not preview the delete'),
    });
  }

  deletePreviewMessage() {
    const p = this.pendingDelete();
    if (!p) return { message: '', warning: null as string | null };
    return formatDeletePreview(p.preview, this.rootDir() ?? '', this.trashDirName() ?? '.trash');
  }

  cancelPendingDelete() {
    this.pendingDelete.set(null);
  }

  confirmPendingDelete() {
    const p = this.pendingDelete();
    if (!p) return;
    this.pendingDelete.set(null);
    this.api.deleteFiles(p.paths).subscribe({
      next: resp => this.applyDeleteResults(resp.results),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Delete failed'),
    });
  }

  private applyDeleteResults(results: DeleteItemResult[], announce = true) {
    const ok = results.filter(r => r.ok);
    const failed = results.filter(r => !r.ok);

    if (ok.length) {
      const okPaths = new Set(ok.map(r => r.path));
      const removed = this.entries().filter(e => okPaths.has(e.path));
      this.entries.update(list => list.filter(e => !okPaths.has(e.path)));
      this.total.update(t => Math.max(0, t - removed.length));
      this.counts.update(c => {
        if (!c) return c;
        const next = { ...c, extensions: { ...c.extensions } };
        for (const e of removed) {
          if (e.type === 'directory') { next.directories = Math.max(0, next.directories - 1); continue; }
          const kind = this.cardKind(e);
          if (kind === 'video') next.video = Math.max(0, next.video - 1);
          else if (kind === 'photo') next.photo = Math.max(0, next.photo - 1);
          else next.untracked = Math.max(0, next.untracked - 1);
          if (e.file_extension && next.extensions[e.file_extension] != null) {
            next.extensions[e.file_extension] = Math.max(0, next.extensions[e.file_extension] - 1);
          }
        }
        return next;
      });
      this.bulkSelected.update(sel => new Set([...sel].filter(path => !okPaths.has(path))));

      // Did we delete the directory we're currently looking at, or an ancestor of it?
      // Nearest existing parent = the deleted path's own parent (the subtree moved as a whole).
      const cur = this.currentPath();
      if (cur) {
        for (const r of ok) {
          if (cur === r.path || cur.startsWith(r.path + '/')) {
            this.navigateTo(this.parentOf(r.path));
            break;
          }
        }
      }

      // Close the open detail panel if it was showing a deleted file.
      const sel = this.selectedFile();
      if (sel && ok.some(r => sel.path === r.path)) this.closeDetails();
    }

    if (!announce) return;
    const okWord = ok.length === 1 ? 'item' : 'items';
    this.showFileOpMessage(`${ok.length} ${okWord} moved to trash`);
    if (failed.length) this.showDeleteFailures(failed);
  }

  private showDeleteFailures(failed: DeleteItemResult[]) {
    const shown = failed.slice(0, 3).map(f => `${this.relativePath(f.path)}: ${f.error ?? 'failed'}`).join('; ');
    const more = failed.length > 3 ? ` and ${failed.length - 3} more` : '';
    this.showFileOpMessage(`${failed.length} failed to move to trash — ${shown}${more}`, true);
  }

  /** The file-detail-panel's own "Move to trash" (preview → confirm → delete
      runs inside the panel, which also shows the toast; this just reconciles the grid, #61). */
  onFileDeleted(path: string) {
    this.closeDetails();
    this.applyDeleteResults([{ path, ok: true }], false);
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
    this.bulkPop.set(null);
    this.selectionAnchor = null;
  }

  /** Click / Enter on a tile. In selection mode, or with Shift/Cmd/Ctrl, it
      selects instead of opening; selection starts without pressing "Select". */
  onCardOpen(entry: PathChild, event: MouseEvent | KeyboardEvent) {
    const modifier = event.shiftKey || event.metaKey || event.ctrlKey;
    if (entry.type === 'file' && (this.bulkMode() || modifier)) {
      this.selectEntry(entry, event.shiftKey);
      return;
    }
    this.onEntryClick(entry);
  }

  onCardToggle(entry: PathChild, event: MouseEvent) {
    this.selectEntry(entry, event.shiftKey);
  }

  private selectEntry(entry: PathChild, range: boolean) {
    if (!this.bulkMode()) this.enterBulkMode();
    const order = this.orderedFiles().map(e => e.path);
    const from = range && this.selectionAnchor ? order.indexOf(this.selectionAnchor) : -1;
    const to = order.indexOf(entry.path);
    if (from >= 0 && to >= 0) {
      const [a, b] = from < to ? [from, to] : [to, from];
      this.bulkSelected.update(sel => {
        const next = new Set(sel);
        order.slice(a, b + 1).forEach(p => next.add(p));
        return next;
      });
    } else {
      this.toggleBulkSelect(entry);
    }
    this.selectionAnchor = entry.path;
  }

  openBulkPop(kind: 'keyword' | 'location' | 'list', anchor: HTMLElement) {
    if (this.bulkPop()?.kind === kind) { this.closeBulkPop(); return; }
    this.locationFilter.set('');
    this.bulkPop.set({ kind, anchor });
    setTimeout(() => document.querySelector<HTMLInputElement>('.quick-form input')?.focus());
  }

  closeBulkPop() {
    this.bulkPop.set(null);
  }

  locationGeo(loc: Location): string {
    return [loc.city, loc.region, loc.country].filter(Boolean).join(', ');
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
    const skipped = this.bulkSelected().size - targets.length;
    this.bulkApplying.set(true);
    this.bulkKeyword.set('');
    this.closeBulkPop();
    forkJoin(targets.map(e => this.api.addKeyword(e.md5_hash!, kw))).subscribe({
      next: () => {
        this.bulkApplying.set(false);
        if (!this.allKeywords().includes(kw)) this.allKeywords.update(list => [...list, kw]);
        this.toast.show(this.withSkipped(`Added “${kw}” to ${this.plural(targets.length, 'file')}`, skipped));
      },
      error: () => {
        this.bulkApplying.set(false);
        this.toast.show(`Couldn't add “${kw}” to every file. Try again.`);
      },
    });
  }

  bulkAssignLocation(locationIdOverride?: number) {
    const locationId = locationIdOverride ?? (+this.bulkLocationId() || null);
    if (!locationId) return;
    const targets = this.bulkTrackedEntries();
    if (!targets.length) return;
    const skipped = this.bulkSelected().size - targets.length;
    const loc = this.allLocations().find(l => l.id === locationId);
    const label = loc ? (loc.name || loc.city || loc.region || loc.country) : 'location';
    this.bulkApplying.set(true);
    this.closeBulkPop();
    forkJoin(targets.map(e => this.api.assignLocation(e.md5_hash!, locationId))).subscribe({
      next: () => {
        this.bulkApplying.set(false);
        this.bulkLocationId.set('');
        this.toast.show(this.withSkipped(`Set ${label} for ${this.plural(targets.length, 'file')}`, skipped));
      },
      error: () => {
        this.bulkApplying.set(false);
        this.bulkLocationId.set('');
        this.toast.show(`Couldn't set the location for every file. Try again.`);
      },
    });
  }

  bulkAddToList(list: FileList) {
    const targets = this.bulkTrackedEntries();
    const untrackedCount = this.bulkSelected().size - targets.length;
    if (!targets.length) return;
    this.bulkApplying.set(true);
    this.closeBulkPop();
    const md5s = targets.map(e => e.md5_hash!);
    this.api.addFilesToList(list.id, md5s).subscribe({
      next: resp => {
        this.bulkApplying.set(false);
        const parts: string[] = [];
        if (resp.added.length) parts.push(`Added ${resp.added.length} to '${list.name}'`);
        if (resp.existing.length) parts.push(`${resp.existing.length} already in list`);
        if (untrackedCount > 0) parts.push(`${untrackedCount} untracked skipped`);
        this.toast.show(parts.join(' · ') || `Nothing added to '${list.name}'`);
      },
      error: () => {
        this.bulkApplying.set(false);
        this.toast.show(`Failed to add to '${list.name}'`);
      },
    });
  }

  private plural(n: number, word: string): string {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  private withSkipped(message: string, skipped: number): string {
    return skipped > 0 ? `${message} · ${skipped} untracked skipped` : message;
  }


  cardKind(entry: PathChild): 'video' | 'photo' | 'other' {
    if (VIDEO_TYPES.includes(entry.media_type as any)) return 'video';
    if (PHOTO_TYPES.includes(entry.media_type as any)) return 'photo';
    return 'other';
  }

  entryPreviewUrl(entry: PathChild): string | null {
    if (!entry.md5_hash) return null;
    if (!VIDEO_TYPES.includes(entry.media_type as any) && !PHOTO_TYPES.includes(entry.media_type as any)) return null;
    // Don't build a URL (no request) unless the preview is actually ready
    // (#77) — null/undefined is treated as 'ok' for backwards-compat.
    if (entry.preview_status && entry.preview_status !== 'ok') return null;
    return this.api.clipPreviewUrl(entry.md5_hash);
  }

  formatLocation(loc: Location): string {
    const geo = [loc.country, loc.region, loc.city].filter(Boolean).join(' › ');
    return loc.name ? `${loc.name} — ${geo}` : geo;
  }
}
