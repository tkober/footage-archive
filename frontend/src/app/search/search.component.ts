import { Component, OnInit, OnDestroy, HostListener, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { Subject, Subscription, forkJoin } from 'rxjs';
import { debounceTime, distinctUntilChanged, map, switchMap } from 'rxjs/operators';

import { ApiService } from '../services/api.service';
import { OpenInService } from '../services/open-in.service';
import { PreviewCacheService } from '../services/preview-cache.service';
import { TaskPollService } from '../services/task-poll.service';
import { ToastService } from '../shared/toast/toast.service';
import { DetailNavItem, FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { MediaCardComponent, MediaCardKind } from '../shared/media-card/media-card.component';
import { MenuComponent, MenuItem, MenuPoint } from '../shared/menu/menu.component';
import { PopoverComponent } from '../shared/popover/popover.component';
import { ListPickerComponent } from '../shared/list-picker/list-picker.component';
import { FolderPickerComponent } from '../shared/folder-picker/folder-picker.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { ComparisonComponent } from '../comparison/comparison.component';
import { LoadMoreFooterComponent } from '../shared/load-more-footer/load-more-footer.component';
import { InfiniteScrollDirective } from '../shared/infinite-scroll/infinite-scroll.directive';
import { IconComponent } from '../shared/icon/icon.component';
import {
  DeleteItemResult, DeletePreviewResponse, FileInfo, FileList, FileSearchQuery, Location,
  MoveItemResult, MovePreviewResponse, PathChild, SearchResponse, SearchResult,
  VIDEO_TYPES, PHOTO_TYPES, formatDeletePreview,
} from '../models';

const SKELETON_CAP = 12;
const FILE_OP_RESULT_TIMEOUT_MS = 6000;

const MEDIA_TYPE_OPTIONS = [
  { value: 'video',       label: 'Video' },
  { value: 'photo',       label: 'Photo' },
  { value: '360_video',   label: '360 Video' },
  { value: '360_photo',   label: '360 Photo' },
];

/** Single-key shortcuts on a focused card (#144, ported from the browser's
    #41) → menu item id. No rename/track here — every result is tracked
    already, and renaming isn't part of this page. */
const SHORTCUTS: Record<string, string> = {
  ' ': 'open', m: 'move', k: 'keyword', l: 'list', r: 'rescan',
  Delete: 'delete', Backspace: 'delete',
};

/** Pending move awaiting confirmation (context menu single entry, or bulk mode). */
interface PendingMove {
  paths: string[];
  targetDirectory: string;
  preview: MovePreviewResponse;
}

/** Pending delete-to-trash awaiting confirmation (context menu single entry, or bulk mode). */
interface PendingDelete {
  paths: string[];
  preview: DeletePreviewResponse;
}

@Component({
  selector: 'app-search',
  standalone: true,
  imports: [
    FormsModule, FileDetailPanelComponent, MediaCardComponent, LoadMoreFooterComponent,
    InfiniteScrollDirective, IconComponent, MenuComponent, PopoverComponent, ListPickerComponent,
    FolderPickerComponent, ConfirmDialogComponent, ComparisonComponent,
  ],
  host: { class: 'page-flush' },
  templateUrl: './search.component.html',
  styleUrl: './search.component.css',
})
export class SearchComponent implements OnInit, OnDestroy {
  readonly api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private openIn = inject(OpenInService);
  private previewCache = inject(PreviewCacheService);
  private taskPoll = inject(TaskPollService);
  private toast = inject(ToastService);
  private subs: Subscription[] = [];
  private filterChange$ = new Subject<void>();
  private facetInput$ = new Subject<{ field: string; q: string }>();

  readonly mediaTypeOptions = MEDIA_TYPE_OPTIONS;

  // ── Filter state ──
  selectedMediaTypes = signal<Set<string>>(new Set());
  selectedKeywords   = signal<string[]>([]);
  country            = signal('');
  dateFrom           = signal('');
  dateTo             = signal('');
  cameraMake         = signal('');
  cameraModel        = signal('');
  videoCodec         = signal('');
  keywordInput       = signal('');
  // Geographic filter set by deep-linking from the map's "open in search" link
  bbox = signal<{ west: number; south: number; east: number; north: number } | null>(null);

  // Lists
  allLists         = signal<FileList[]>([]);
  selectedListIds  = signal<Set<number>>(new Set());
  listCode         = signal('');
  showSingleListCode = computed(() => this.selectedListIds().size === 1);

  // ── Facet suggestion lists ──
  countrySuggestions     = signal<string[]>([]);
  cameraMakeSuggestions  = signal<string[]>([]);
  cameraModelSuggestions = signal<string[]>([]);
  videoCodecSuggestions  = signal<string[]>([]);
  allKeywords            = signal<string[]>([]);

  // ── Results ──
  results        = signal<SearchResult[]>([]);
  total          = signal(0);
  currentPage    = signal(1);
  loading        = signal(false);
  loadMoreError  = signal<string | null>(null);
  hasFilters     = signal(false);
  /** Phone: the filter panel is a collapsible sheet above the results. */
  filtersOpen    = signal(false);

  // Archive root / trash folder name (#144) — for the folder picker and the
  // delete preview's "will be moved to …" copy, same as the browser (#61).
  rootDir      = signal<string | null>(null);
  trashDirName = signal<string | null>(null);

  // ── Selection / bulk mode (#144, ported from the browser's #42) ──
  bulkMode       = signal(false);
  /** Keyed by `md5_hash` — every search result is tracked and always has one. */
  bulkSelected   = signal<Set<string>>(new Set());
  bulkKeyword    = signal('');
  bulkLocationId = signal('');
  bulkApplying   = signal(false);
  allLocations   = signal<Location[]>([]);
  /** Last tile clicked in selection mode; Shift-click selects up to here. */
  private selectionAnchor: string | null = null;
  /** Keyword / location / list form above the floating bulk bar. */
  bulkPop        = signal<{ kind: 'keyword' | 'location' | 'list'; anchor: HTMLElement } | null>(null);
  locationFilter = signal('');

  // Comparison view
  showComparison = signal(false);

  // Context menu (right-click or the card's "⋯"), same pattern as the browser.
  menuEntry  = signal<SearchResult | null>(null);
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

  // "Add keyword…" / "Add to list…" popover anchored at the card
  quickPop     = signal<{ kind: 'keyword' | 'list'; entry: SearchResult; anchor: HTMLElement } | null>(null);
  quickKeyword = signal('');

  // Move to… (folder picker + confirm), shared by the context menu and bulk mode
  movePickerPaths = signal<string[] | null>(null);
  pendingMove     = signal<PendingMove | null>(null);
  moveError       = signal<string | null>(null);

  // Move to trash, shared by the context menu, the keyboard shortcut and bulk mode
  pendingDelete = signal<PendingDelete | null>(null);

  /** Every active filter as a removable chip above the results (#44). */
  activeFilters = computed<{ id: string; label: string; remove: () => void }[]>(() => {
    const out: { id: string; label: string; remove: () => void }[] = [];
    for (const t of this.selectedMediaTypes()) {
      out.push({ id: 'type:' + t, label: MEDIA_TYPE_OPTIONS.find(o => o.value === t)?.label ?? t, remove: () => this.toggleMediaType(t) });
    }
    for (const kw of this.selectedKeywords()) out.push({ id: 'kw:' + kw, label: '#' + kw, remove: () => this.removeKeyword(kw) });
    if (this.country()) out.push({ id: 'country', label: this.country(), remove: () => this.clearFacet('country') });
    if (this.dateFrom() || this.dateTo()) {
      out.push({
        id: 'date',
        label: this.dateFrom() && this.dateTo() ? `${this.dateFrom()} – ${this.dateTo()}`
             : this.dateFrom() ? `from ${this.dateFrom()}` : `until ${this.dateTo()}`,
        remove: () => { this.dateFrom.set(''); this.dateTo.set(''); this.onFilterChange(); },
      });
    }
    if (this.cameraMake()) out.push({ id: 'make', label: this.cameraMake(), remove: () => this.clearFacet('cameraMake') });
    if (this.cameraModel()) out.push({ id: 'model', label: this.cameraModel(), remove: () => this.clearFacet('cameraModel') });
    if (this.videoCodec()) out.push({ id: 'codec', label: this.videoCodec(), remove: () => this.clearFacet('videoCodec') });
    for (const id of this.selectedListIds()) {
      const name = this.allLists().find(l => l.id === id)?.name ?? `List ${id}`;
      out.push({ id: 'list:' + id, label: name, remove: () => this.toggleList(id) });
    }
    if (this.listCode()) out.push({ id: 'code', label: 'Code ' + this.listCode(), remove: () => this.onListCodeInput('') });
    if (this.bbox()) out.push({ id: 'bbox', label: 'Map area', remove: () => this.clearBbox() });
    return out;
  });
  selectedFile   = signal<FileInfo | null>(null);
  loadingDetails = signal(false);

  readonly PAGE_SIZE = 50;

  hasMore = computed(() => this.results().length < this.total());

  /** `loading` covers both the initial search and a load-more fetch; it's a
      load-more only once results are already non-empty, which is also when
      skeleton tiles should appear (#40). */
  skeletonCount = computed(() =>
    this.loading() && this.results().length > 0
      ? Math.min(SKELETON_CAP, this.PAGE_SIZE, this.total() - this.results().length)
      : 0
  );

  /** Last loaded result's kind decides which grid gets the skeletons. */
  skeletonTarget = computed<MediaCardKind | null>(() => {
    if (!this.skeletonCount()) return null;
    const last = this.results().at(-1);
    return last ? this.cardKind(last) : null;
  });

  skeletons = computed(() => Array.from({ length: this.skeletonCount() }, (_, i) => i));

  // Keyword suggestions filtered from allKeywords
  keywordSuggestions = computed(() => {
    const input = this.keywordInput().toLowerCase();
    const applied = new Set(this.selectedKeywords());
    return this.allKeywords().filter(
      kw => !applied.has(kw) && (input === '' || kw.toLowerCase().includes(input))
    );
  });

  // Show technical filters based on selected media types
  showPhotoFilters = computed(() => {
    const types = this.selectedMediaTypes();
    return types.size === 0 || PHOTO_TYPES.some(t => types.has(t));
  });

  showVideoFilters = computed(() => {
    const types = this.selectedMediaTypes();
    return types.size === 0 || VIDEO_TYPES.some(t => types.has(t));
  });

  // Split results into video and photo sections for the grid
  videoResults = computed(() =>
    this.results().filter(r => VIDEO_TYPES.includes(r.media_type as any))
  );

  photoResults = computed(() =>
    this.results().filter(r => PHOTO_TYPES.includes(r.media_type as any))
  );

  /** Results in the order they're rendered (videos, then stills): the range for Shift-click. */
  orderedResults = computed(() => [...this.videoResults(), ...this.photoResults()]);

  bulkCount = computed(() => this.bulkSelected().size);

  filteredLocations = computed(() => {
    const q = this.locationFilter().trim().toLowerCase();
    const locs = this.allLocations();
    return q ? locs.filter(l => this.locationGeo(l).toLowerCase().includes(q) || (l.name ?? '').toLowerCase().includes(q)) : locs;
  });

  // Selected, tracked photos eligible for the comparison view.
  comparablePhotos = computed<PathChild[]>(() => {
    const sel = this.bulkSelected();
    return this.photoResults().filter(r => sel.has(r.md5_hash)).map(r => this.toPathChild(r));
  });

  showDetail = computed(() => this.loadingDetails() || !!this.selectedFile());

  ngOnInit(): void {
    this.api.getAllKeywords().subscribe(kws => this.allKeywords.set(kws));
    this.api.getLists().subscribe(lists => this.allLists.set(lists));
    this.api.getConfig().subscribe(config => {
      this.rootDir.set(config.root_dir);
      this.trashDirName.set(config.trash_dir_name);
    });

    // Debounced re-search on any filter change
    this.subs.push(
      this.filterChange$.pipe(debounceTime(400)).subscribe(() => {
        this.currentPage.set(1);
        this.runSearch(false);
      })
    );

    // Debounced facet typeahead
    this.subs.push(
      this.facetInput$.pipe(
        debounceTime(300),
        distinctUntilChanged((a, b) => a.field === b.field && a.q === b.q),
        switchMap(({ field, q }) =>
          this.api.getFacetValues(field, q).pipe(map(values => ({ field, values })))
        ),
      ).subscribe(({ field, values }) => {
        if (field === 'country')      this.countrySuggestions.set(values);
        if (field === 'camera_make')  this.cameraMakeSuggestions.set(values);
        if (field === 'camera_model') this.cameraModelSuggestions.set(values);
        if (field === 'video_codec')  this.videoCodecSuggestions.set(values);
      })
    );

    // Apply a geographic filter passed via query params (map → "open in search").
    const qp = this.route.snapshot.queryParamMap;
    const w = qp.get('bbox_west'), s = qp.get('bbox_south');
    const e = qp.get('bbox_east'), n = qp.get('bbox_north');
    if (w != null && s != null && e != null && n != null) {
      this.bbox.set({ west: +w, south: +s, east: +e, north: +n });
      this.onFilterChange();
    }

    // Apply a list/code filter passed via query params (deep link, analogous to bbox).
    const listParams = qp.getAll('list');
    const code = qp.get('code');
    if (listParams.length || code) {
      if (listParams.length) {
        this.selectedListIds.set(new Set(listParams.map(id => +id)));
      }
      if (code) {
        this.listCode.set(code.toUpperCase());
      }
      this.onFilterChange();
    }
  }

  clearBbox(): void {
    this.bbox.set(null);
    this.router.navigate([], { relativeTo: this.route, queryParams: {} });
    this.onFilterChange();
  }

  ngOnDestroy(): void {
    this.subs.forEach(s => s.unsubscribe());
  }

  // ── Filter change handlers ──

  toggleMediaType(value: string): void {
    const types = new Set(this.selectedMediaTypes());
    types.has(value) ? types.delete(value) : types.add(value);
    this.selectedMediaTypes.set(types);
    this.onFilterChange();
  }

  toggleList(id: number): void {
    const ids = new Set(this.selectedListIds());
    ids.has(id) ? ids.delete(id) : ids.add(id);
    this.selectedListIds.set(ids);
    if (ids.size !== 1) this.listCode.set('');
    this.onFilterChange();
  }

  onListCodeInput(value: string): void {
    this.listCode.set(value.toUpperCase());
    this.onFilterChange();
  }

  addKeyword(kw: string): void {
    kw = kw.trim();
    if (!kw || this.selectedKeywords().includes(kw)) return;
    this.selectedKeywords.set([...this.selectedKeywords(), kw]);
    this.keywordInput.set('');
    this.onFilterChange();
  }

  removeKeyword(kw: string): void {
    this.selectedKeywords.set(this.selectedKeywords().filter(k => k !== kw));
    this.onFilterChange();
  }

  onFacetInput(field: string, q: string): void {
    this.facetInput$.next({ field, q });
  }

  clearAllFilters(): void {
    this.selectedMediaTypes.set(new Set());
    this.selectedKeywords.set([]);
    this.country.set('');
    this.dateFrom.set('');
    this.dateTo.set('');
    this.cameraMake.set('');
    this.cameraModel.set('');
    this.videoCodec.set('');
    this.selectedListIds.set(new Set());
    this.listCode.set('');
    if (this.bbox()) this.clearBbox();
    this.onFilterChange();
  }

  onFilterChange(): void {
    const hasAny =
      this.selectedMediaTypes().size > 0 ||
      this.selectedKeywords().length > 0 ||
      !!this.country() ||
      !!this.dateFrom() ||
      !!this.dateTo() ||
      !!this.cameraMake() ||
      !!this.cameraModel() ||
      !!this.videoCodec() ||
      !!this.bbox() ||
      this.selectedListIds().size > 0 ||
      !!this.listCode();
    this.hasFilters.set(hasAny);
    this.loadMoreError.set(null);
    if (hasAny) this.filterChange$.next();
    else {
      this.results.set([]);
      this.total.set(0);
      this.pruneSelection(new Set());
    }
  }

  clearFacet(field: 'country' | 'cameraMake' | 'cameraModel' | 'videoCodec'): void {
    if (field === 'country')     this.country.set('');
    if (field === 'cameraMake')  this.cameraMake.set('');
    if (field === 'cameraModel') this.cameraModel.set('');
    if (field === 'videoCodec')  this.videoCodec.set('');
    this.onFilterChange();
  }

  loadMore(): void {
    // Guards against a duplicate fetch of the same page, and keeps
    // auto-loading paused while a previous load-more error awaits retry.
    if (this.loading() || this.loadMoreError() || !this.hasMore()) return;
    this.currentPage.set(this.currentPage() + 1);
    this.runSearch(true);
  }

  retryLoadMore(): void {
    this.loadMoreError.set(null);
    this.loadMore();
  }

  private buildQuery(page: number): FileSearchQuery {
    return {
      media_types: [...this.selectedMediaTypes()],
      keywords:    this.selectedKeywords(),
      country:     this.country() || null,
      date_from:   this.dateFrom() || null,
      date_to:     this.dateTo() || null,
      camera_make:  this.cameraMake() || null,
      camera_model: this.cameraModel() || null,
      video_codec:  this.videoCodec() || null,
      bbox_west:   this.bbox()?.west  ?? null,
      bbox_south:  this.bbox()?.south ?? null,
      bbox_east:   this.bbox()?.east  ?? null,
      bbox_north:  this.bbox()?.north ?? null,
      list_ids:    [...this.selectedListIds()],
      // With no list selected (e.g. a code-only deep link) the backend matches
      // the code in any list; with several lists the code input is hidden.
      list_code:   this.selectedListIds().size <= 1 ? (this.listCode() || null) : null,
      page,
      page_size:   this.PAGE_SIZE,
    };
  }

  private runSearch(append: boolean): void {
    this.loading.set(true);
    const query = this.buildQuery(this.currentPage());
    this.api.searchFiles(query).subscribe({
      next: (resp: SearchResponse) => {
        this.total.set(resp.total);
        const items = append ? [...this.results(), ...resp.items] : resp.items;
        this.results.set(items);
        this.loading.set(false);
        // A fresh (non-append) search replaces the results — prune the
        // selection down to hashes still present. Loading more pages keeps it.
        if (!append) this.pruneSelection(new Set(items.map(r => r.md5_hash)));
        // A code search that resolves to exactly one result opens its detail directly.
        if (!append && query.list_code && resp.items.length === 1) {
          this.selectResult(resp.items[0]);
        }
      },
      error: () => {
        this.loading.set(false);
        if (append) {
          this.currentPage.update(p => p - 1);
          this.loadMoreError.set('Failed to load more.');
        }
      },
    });
  }

  private pruneSelection(validHashes: Set<string>): void {
    this.bulkSelected.update(sel => new Set([...sel].filter(h => validHashes.has(h))));
  }

  /** Detail neighbours (#43): loaded results of the same kind as the open file. */
  private detailSiblings = computed(() => {
    const cur = this.selectedFile();
    if (!cur) return [];
    return VIDEO_TYPES.includes(cur.media_type as any) ? this.videoResults() : this.photoResults();
  });
  detailNavItems = computed<DetailNavItem[]>(() => this.detailSiblings().map(r => ({
    key: r.md5_hash ?? r.directory + '/' + r.file_name,
    label: r.file_name,
    previewUrl: this.entryPreviewUrl(r),
    video: this.cardKind(r) === 'video',
  })));
  detailNavIndex = computed(() => {
    const cur = this.selectedFile();
    return cur ? this.detailSiblings().findIndex(r => r.md5_hash === cur.md5_hash) : -1;
  });

  jumpDetail(index: number): void {
    const target = this.detailSiblings()[index];
    if (target) this.selectResult(target, true);
  }

  /** `keep`: stepping between neighbours, so leave the current file up until
      the next one has loaded (no flash). */
  selectResult(result: SearchResult, keep = false): void {
    const path = this.resultPath(result);
    if (!keep) this.selectedFile.set(null);
    this.loadingDetails.set(true);
    this.api.getFileDetails(path).subscribe({
      next: info => { this.selectedFile.set(info); this.loadingDetails.set(false); },
      error: () => this.loadingDetails.set(false),
    });
  }

  closeDetail(): void {
    this.selectedFile.set(null);
    this.loadingDetails.set(false);
  }

  @HostListener('document:keydown.escape', ['$event'])
  onEscapeKey(event: Event): void {
    // A menu/popover handles its own Esc (and marks the event handled).
    if (event.defaultPrevented || this.menuEntry() || this.quickPop()) return;
    if (this.showComparison()) this.closeComparison();
    else if (this.showDetail()) this.closeDetail();
    else if (this.bulkMode()) this.exitBulkMode();
  }

  /** The panel's own "Move to trash" (#61): drop the deleted result from the
      grid without a full reload and close the panel if it was showing it.
      Generalised into `applyDeleteResults` so the bulk delete path shares it. */
  onFileDeleted(path: string): void {
    this.applyDeleteResults([{ path, ok: true }], false);
  }

  cardKind(result: SearchResult): MediaCardKind {
    return VIDEO_TYPES.includes(result.media_type as any) ? 'video' : 'photo';
  }

  cardExtension(result: SearchResult): string | null {
    const dot = result.file_name.lastIndexOf('.');
    return dot > 0 ? result.file_name.slice(dot + 1) : null;
  }

  /** Don't build a preview URL (no request) unless the preview is actually
      ready (#77) — null/undefined is treated as 'ok' for backwards-compat. */
  entryPreviewUrl(result: SearchResult): string | null {
    if (!result.md5_hash) return null;
    if (result.preview_status && result.preview_status !== 'ok') return null;
    return this.api.clipPreviewUrl(result.md5_hash);
  }

  private resultPath(result: SearchResult): string {
    return result.directory + '/' + result.file_name;
  }

  private toPathChild(result: SearchResult): PathChild {
    const ext = this.cardExtension(result);
    return {
      name: result.file_name,
      path: this.resultPath(result),
      type: 'file',
      file_extension: ext ? `.${ext}` : null,
      tracked: true,
      md5_hash: result.md5_hash,
      media_type: result.media_type as any,
      preview_status: result.preview_status ?? null,
    };
  }

  relativePath(path: string): string {
    const root = this.rootDir();
    if (!root) return path;
    if (path === root) return '/';
    return path.startsWith(root) ? path.slice(root.length).replace(/^\/+/, '') : path;
  }

  private showFileOpMessage(message: string, sticky = false): void {
    this.toast.show(message, { duration: sticky ? 0 : FILE_OP_RESULT_TIMEOUT_MS });
  }

  // ── Selection / bulk mode (#144) ──

  enterBulkMode(): void {
    this.bulkMode.set(true);
    if (!this.allLocations().length)
      this.api.getLocations().subscribe({ next: locs => this.allLocations.set(locs) });
  }

  exitBulkMode(): void {
    this.bulkMode.set(false);
    this.bulkSelected.set(new Set());
    this.bulkKeyword.set('');
    this.bulkLocationId.set('');
    this.bulkPop.set(null);
    this.selectionAnchor = null;
  }

  /** Click / Enter on a tile. In selection mode, or with Shift/Cmd/Ctrl, it
      selects instead of opening; selection starts without pressing "Select". */
  onCardOpen(result: SearchResult, event: MouseEvent | KeyboardEvent): void {
    const modifier = event.shiftKey || event.metaKey || event.ctrlKey;
    if (this.bulkMode() || modifier) {
      this.selectEntry(result, event.shiftKey);
      return;
    }
    this.selectResult(result);
  }

  onCardToggle(result: SearchResult, event: MouseEvent): void {
    this.selectEntry(result, event.shiftKey);
  }

  private selectEntry(result: SearchResult, range: boolean): void {
    if (!this.bulkMode()) this.enterBulkMode();
    const order = this.orderedResults().map(r => r.md5_hash);
    const from = range && this.selectionAnchor ? order.indexOf(this.selectionAnchor) : -1;
    const to = order.indexOf(result.md5_hash);
    if (from >= 0 && to >= 0) {
      const [a, b] = from < to ? [from, to] : [to, from];
      this.bulkSelected.update(sel => {
        const next = new Set(sel);
        order.slice(a, b + 1).forEach(h => next.add(h));
        return next;
      });
    } else {
      this.toggleBulkSelect(result);
    }
    this.selectionAnchor = result.md5_hash;
  }

  toggleBulkSelect(result: SearchResult): void {
    this.bulkSelected.update(s => {
      const next = new Set(s);
      next.has(result.md5_hash) ? next.delete(result.md5_hash) : next.add(result.md5_hash);
      return next;
    });
  }

  isBulkSelected(result: SearchResult): boolean {
    return this.bulkSelected().has(result.md5_hash);
  }

  clearBulkSelection(): void {
    this.bulkSelected.set(new Set());
  }

  selectAllBulk(): void {
    this.bulkSelected.set(new Set(this.orderedResults().map(r => r.md5_hash)));
  }

  private selectedResults(): SearchResult[] {
    const sel = this.bulkSelected();
    return this.results().filter(r => sel.has(r.md5_hash));
  }

  openBulkPop(kind: 'keyword' | 'location' | 'list', anchor: HTMLElement): void {
    if (this.bulkPop()?.kind === kind) { this.closeBulkPop(); return; }
    this.locationFilter.set('');
    this.bulkPop.set({ kind, anchor });
    setTimeout(() => document.querySelector<HTMLInputElement>('.quick-form input')?.focus());
  }

  closeBulkPop(): void {
    this.bulkPop.set(null);
  }

  locationGeo(loc: Location): string {
    return [loc.city, loc.region, loc.country].filter(Boolean).join(', ');
  }

  openComparison(): void {
    if (this.comparablePhotos().length >= 2) this.showComparison.set(true);
  }

  closeComparison(): void {
    this.showComparison.set(false);
  }

  bulkAddKeyword(): void {
    const kw = this.bulkKeyword().trim();
    const targets = this.selectedResults();
    if (!kw || !targets.length) return;
    this.bulkApplying.set(true);
    this.bulkKeyword.set('');
    this.closeBulkPop();
    forkJoin(targets.map(r => this.api.addKeyword(r.md5_hash, kw))).subscribe({
      next: () => {
        this.bulkApplying.set(false);
        if (!this.allKeywords().includes(kw)) this.allKeywords.update(list => [...list, kw]);
        this.toast.show(`Added “${kw}” to ${this.plural(targets.length, 'file')}`);
      },
      error: () => {
        this.bulkApplying.set(false);
        this.toast.show(`Couldn't add “${kw}” to every file. Try again.`);
      },
    });
  }

  bulkAssignLocation(locationIdOverride?: number): void {
    const locationId = locationIdOverride ?? (+this.bulkLocationId() || null);
    if (!locationId) return;
    const targets = this.selectedResults();
    if (!targets.length) return;
    const loc = this.allLocations().find(l => l.id === locationId);
    const label = loc ? (loc.name || loc.city || loc.region || loc.country) : 'location';
    this.bulkApplying.set(true);
    this.closeBulkPop();
    forkJoin(targets.map(r => this.api.assignLocation(r.md5_hash, locationId))).subscribe({
      next: () => {
        this.bulkApplying.set(false);
        this.bulkLocationId.set('');
        this.toast.show(`Set ${label} for ${this.plural(targets.length, 'file')}`);
      },
      error: () => {
        this.bulkApplying.set(false);
        this.bulkLocationId.set('');
        this.toast.show(`Couldn't set the location for every file. Try again.`);
      },
    });
  }

  bulkAddToList(list: FileList): void {
    const targets = this.selectedResults();
    if (!targets.length) return;
    this.bulkApplying.set(true);
    this.closeBulkPop();
    const md5s = targets.map(r => r.md5_hash);
    this.api.addFilesToList(list.id, md5s).subscribe({
      next: resp => {
        this.bulkApplying.set(false);
        const parts: string[] = [];
        if (resp.added.length) parts.push(`Added ${resp.added.length} to '${list.name}'`);
        if (resp.existing.length) parts.push(`${resp.existing.length} already in list`);
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

  // ── Rescan (#64) ──

  bulkRescan(): void {
    const targets = this.selectedResults();
    if (!targets.length) return;
    this.startRescan(targets.map(r => r.md5_hash));
  }

  /** Starts POST /tracking/refresh for the given hashes (context menu, bulk
      bar) and, once the task finishes, cache-busts their preview URL so the
      grid/detail panel pick up the regenerated preview without a reload. */
  private startRescan(md5Hashes: string[]): void {
    if (!md5Hashes.length) return;
    this.api.refreshFiles(md5Hashes).subscribe({
      next: taskId => {
        this.api.taskRefresh$.next();
        this.toast.show('Rescan started — see tasks.');
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

  // ── Move to… (folder picker + confirm) ──

  bulkMoveTo(): void {
    const targets = this.selectedResults();
    if (!targets.length) return;
    this.openMovePicker(targets.map(r => this.resultPath(r)));
  }

  openMovePicker(paths: string[]): void {
    this.moveError.set(null);
    this.movePickerPaths.set(paths);
  }

  closeMovePicker(): void {
    this.movePickerPaths.set(null);
  }

  onFolderPicked(targetDirectory: string): void {
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

  confirmPendingMove(): void {
    const p = this.pendingMove();
    if (!p) return;
    this.pendingMove.set(null);
    this.api.moveFiles(p.paths, p.targetDirectory).subscribe({
      next: results => this.applyMoveResults(results),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Move failed'),
    });
  }

  cancelPendingMove(): void {
    this.pendingMove.set(null);
  }

  /** Reconciles the results grid without a full reload (#144): each moved
      result's `directory` is updated from the move's `new_path`, and the
      open detail panel (if it's showing a moved file) is reloaded from the
      new path. */
  private applyMoveResults(moveResults: MoveItemResult[]): void {
    const ok = moveResults.filter(r => r.ok);
    const failed = moveResults.filter(r => !r.ok);

    let message = `${ok.length} moved`;
    if (failed.length) {
      message += ` · ${failed.length} failed: ` + failed.map(f => f.error).join('; ');
    }
    this.showFileOpMessage(message, failed.length > 0);

    if (ok.length) {
      const newDirByPath = new Map<string, string>();
      for (const r of ok) {
        if (!r.new_path) continue;
        const idx = r.new_path.lastIndexOf('/');
        newDirByPath.set(r.path, idx > 0 ? r.new_path.slice(0, idx) : '/');
      }
      this.results.update(list => list.map(res => {
        const newDir = newDirByPath.get(this.resultPath(res));
        return newDir ? { ...res, directory: newDir } : res;
      }));

      // Did we move the file the detail panel is showing?
      const sel = this.selectedFile();
      if (sel) {
        const moved = ok.find(r => r.new_path && r.path === sel.path);
        if (moved?.new_path) {
          this.api.getFileDetails(moved.new_path).subscribe({
            next: info => this.selectedFile.set(info),
            error: () => this.closeDetail(),
          });
        }
      }
    }
  }

  // ── Move to trash (#61) ──

  bulkDelete(): void {
    const targets = this.selectedResults();
    if (!targets.length) return;
    this.openDeletePreview(targets.map(r => this.resultPath(r)));
  }

  /** Always previews first; a failed preview toasts the error and never opens
      the confirm dialog (unlike move, there's no fallback-without-confirm). */
  openDeletePreview(paths: string[]): void {
    if (!paths.length) return;
    this.api.previewDelete(paths).subscribe({
      next: preview => this.pendingDelete.set({ paths, preview }),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Could not preview the delete'),
    });
  }

  deletePreviewMessage(): { message: string; warning: string | null } {
    const p = this.pendingDelete();
    if (!p) return { message: '', warning: null };
    return formatDeletePreview(p.preview, this.rootDir() ?? '', this.trashDirName() ?? '.trash');
  }

  cancelPendingDelete(): void {
    this.pendingDelete.set(null);
  }

  confirmPendingDelete(): void {
    const p = this.pendingDelete();
    if (!p) return;
    this.pendingDelete.set(null);
    this.api.deleteFiles(p.paths).subscribe({
      next: resp => this.applyDeleteResults(resp.results),
      error: err => this.showFileOpMessage(err.error?.detail ?? 'Delete failed'),
    });
  }

  private applyDeleteResults(deleteResults: DeleteItemResult[], announce = true): void {
    const ok = deleteResults.filter(r => r.ok);
    const failed = deleteResults.filter(r => !r.ok);

    if (ok.length) {
      const okPaths = new Set(ok.map(r => r.path));
      const removedHashes = new Set(
        this.results().filter(r => okPaths.has(this.resultPath(r))).map(r => r.md5_hash)
      );
      this.results.update(list => list.filter(r => !okPaths.has(this.resultPath(r))));
      this.total.update(t => Math.max(0, t - removedHashes.size));
      this.bulkSelected.update(sel => new Set([...sel].filter(h => !removedHashes.has(h))));

      // Close the open detail panel if it was showing a deleted file.
      const sel = this.selectedFile();
      if (sel && ok.some(r => r.path === sel.path)) this.closeDetail();
    }

    if (!announce) return;
    const okWord = ok.length === 1 ? 'item' : 'items';
    this.showFileOpMessage(`${ok.length} ${okWord} moved to trash`);
    if (failed.length) this.showDeleteFailures(failed);
  }

  private showDeleteFailures(failed: DeleteItemResult[]): void {
    const shown = failed.slice(0, 3).map(f => `${this.relativePath(f.path)}: ${f.error ?? 'failed'}`).join('; ');
    const more = failed.length > 3 ? ` and ${failed.length - 3} more` : '';
    this.showFileOpMessage(`${failed.length} failed to move to trash — ${shown}${more}`, true);
  }

  // ── Context menu (#41) + keyboard shortcuts on a focused card ──

  onEntryContextMenu(event: MouseEvent, entry: SearchResult): void {
    event.preventDefault();
    event.stopPropagation();
    const source = (event.target as Element | null)?.closest('app-media-card') as HTMLElement | null;
    this.openMenu(entry, { point: { x: event.clientX, y: event.clientY }, source });
  }

  onCardMore(entry: SearchResult, anchor: HTMLElement): void {
    this.openMenu(entry, { anchor, source: anchor.closest('app-media-card') as HTMLElement | null });
  }

  private openMenu(entry: SearchResult, opts: { point?: MenuPoint; anchor?: HTMLElement; source?: HTMLElement | null }): void {
    this.quickPop.set(null);
    this.menuSource = opts.source ?? opts.anchor ?? null;
    this.menuPoint.set(opts.point ?? null);
    this.menuAnchor.set(opts.anchor ?? null);
    this.menuEntry.set(entry);
  }

  /** Closed without picking (Esc, click outside): give focus back to the card. */
  closeMenu(): void {
    const hadMenu = !!this.menuEntry();
    this.menuEntry.set(null);
    this.menuAnchor.set(null);
    this.menuPoint.set(null);
    if (hadMenu) this.focusSource(this.menuSource);
  }

  onMenuSelect(id: string): void {
    const entry = this.menuEntry();
    const source = this.menuSource;
    this.menuEntry.set(null);
    if (entry) this.runEntryAction(id, entry, source);
  }

  /** Menu entries for a result. The same ids drive the keyboard shortcuts on
      a focused card (see `onShortcut`). No Rename/Track — every result is
      already tracked, and renaming isn't part of this page (#144). */
  menuItemsFor(entry: SearchResult): MenuItem[] {
    return [
      { id: 'open', label: 'Open', icon: 'eye', shortcut: 'Space' },
      ...this.openIn.appsFor(this.cardExtension(entry)).map((app, i) => ({
        id: `open-in:${app.id}`, label: `Open in ${app.label}`, icon: app.icon, shortcut: i === 0 ? 'P' : undefined,
      })),
      { id: 'show-in-folder', label: 'Show in folder', icon: 'folder' },
      { id: 'rescan', label: 'Rescan', icon: 'scan', shortcut: 'R', separatorBefore: true },
      { id: 'keyword', label: 'Add keyword…', icon: 'tag', shortcut: 'K', separatorBefore: true },
      { id: 'list', label: 'Add to list…', icon: 'list', shortcut: 'L' },
      { id: 'move', label: 'Move to…', icon: 'move', shortcut: 'M', separatorBefore: true },
      { id: 'copy', label: 'Copy path', icon: 'copy' },
      { id: 'delete', label: 'Move to trash', icon: 'trash', danger: true, separatorBefore: true, shortcut: 'Delete' },
    ];
  }

  /** "Video · MOV" / "Still · RW2" — no duration, unlike the browser, since
      `SearchResult` doesn't carry it. */
  entryTypeLabel(entry: SearchResult): string {
    const ext = (this.cardExtension(entry) ?? '').toUpperCase();
    return this.cardKind(entry) === 'video' ? ['Video', ext].filter(Boolean).join(' · ') : ['Still', ext].filter(Boolean).join(' · ');
  }

  private runEntryAction(id: string, entry: SearchResult, source: HTMLElement | null): void {
    if (id.startsWith('open-in:')) { this.openInApp(id.slice('open-in:'.length), entry); return; }
    switch (id) {
      case 'open':
        this.selectResult(entry);
        break;
      case 'show-in-folder':
        this.router.navigate(['/browser'], { queryParams: { path: entry.directory } });
        break;
      case 'rescan':
        this.startRescan([entry.md5_hash]);
        break;
      case 'move':
        this.openMovePicker([this.resultPath(entry)]);
        break;
      case 'copy':
        this.copyPath(this.resultPath(entry));
        this.focusSource(source);
        break;
      case 'delete':
        this.openDeletePreview([this.resultPath(entry)]);
        break;
      case 'keyword':
      case 'list':
        this.openQuickPop(id, entry, source);
        break;
    }
  }

  /** "Open in <App>" (#126): hands the file off to the device's Opener (#97/#125). */
  private openInApp(appId: string, entry: SearchResult): void {
    const app = this.openIn.apps.find(a => a.id === appId);
    if (!app) return;
    if (this.openIn.open(app, this.resultPath(entry))) {
      this.toast.show(`Opening ${entry.file_name} in ${app.label}…`);
    } else {
      this.toast.show(`Couldn't open ${entry.file_name}: path is outside the archive root`);
    }
  }

  @HostListener('document:keydown', ['$event'])
  onShortcut(ev: Event): void {
    const event = ev as KeyboardEvent;
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (this.menuEntry() || this.quickPop() || this.bulkPop() || this.showDetail() || this.showComparison()
        || this.movePickerPaths() || this.pendingMove() || this.pendingDelete()) return;
    const target = event.target as HTMLElement | null;
    if (!target || target.closest('input, textarea, select, [contenteditable="true"]')) return;
    const host = target.closest('[data-md5]') as HTMLElement | null;
    const entry = host && this.results().find(r => r.md5_hash === host.dataset['md5']);
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

  private openQuickPop(kind: 'keyword' | 'list', entry: SearchResult, source: HTMLElement | null): void {
    const host = source ?? (document.querySelector(`[data-md5="${CSS.escape(entry.md5_hash)}"]`) as HTMLElement | null);
    // app-media-card's host has no box of its own; anchor to the visible tile.
    const anchor = host?.querySelector<HTMLElement>('.card') ?? host;
    if (!anchor) return;
    this.quickKeyword.set('');
    this.quickPop.set({ kind, entry, anchor });
    setTimeout(() => document.querySelector<HTMLInputElement>('.quick-form input')?.focus());
  }

  closeQuickPop(): void {
    const pop = this.quickPop();
    this.quickPop.set(null);
    if (pop) this.focusSource(pop.anchor);
  }

  submitQuickKeyword(): void {
    const pop = this.quickPop();
    const kw = this.quickKeyword().trim();
    if (!pop || !kw) return;
    this.api.addKeyword(pop.entry.md5_hash, kw).subscribe({
      next: () => {
        this.toast.show(`Added “${kw}” to ${pop.entry.file_name}`);
        if (!this.allKeywords().includes(kw)) this.allKeywords.update(list => [...list, kw]);
      },
      error: () => this.toast.show(`Couldn't add “${kw}” to ${pop.entry.file_name}`),
    });
    this.closeQuickPop();
  }

  quickAddToList(list: FileList): void {
    const pop = this.quickPop();
    if (!pop) return;
    this.api.addFilesToList(list.id, [pop.entry.md5_hash]).subscribe({
      next: resp => this.toast.show(resp.added.length
        ? `Added ${pop.entry.file_name} to ‘${list.name}’`
        : `${pop.entry.file_name} is already in ‘${list.name}’`),
      error: () => this.toast.show(`Couldn't add ${pop.entry.file_name} to ‘${list.name}’`),
    });
    this.closeQuickPop();
  }

  /** navigator.clipboard only exists in secure contexts; the NAS is usually
      reached over plain http, so fall back to a hidden textarea. */
  private copyPath(path: string): void {
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

  private focusSource(el: HTMLElement | null): void {
    if (!el || !el.isConnected) return;
    (el.matches('[tabindex], button') ? el : el.querySelector<HTMLElement>('[tabindex], button'))?.focus();
  }
}
