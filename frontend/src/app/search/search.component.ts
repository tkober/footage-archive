import { Component, OnInit, OnDestroy, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Subject, Subscription } from 'rxjs';
import { debounceTime, distinctUntilChanged, map, switchMap } from 'rxjs/operators';

import { ApiService } from '../services/api.service';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { FileInfo, FileSearchQuery, SearchResponse, SearchResult, VIDEO_TYPES, PHOTO_TYPES } from '../models';

const MEDIA_TYPE_OPTIONS = [
  { value: 'video',       label: 'Video' },
  { value: 'photo',       label: 'Photo' },
  { value: '360_video',   label: '360 Video' },
  { value: '360_photo',   label: '360 Photo' },
];

@Component({
  selector: 'app-search',
  standalone: true,
  imports: [FormsModule, FileDetailPanelComponent],
  templateUrl: './search.component.html',
  styleUrl: './search.component.css',
})
export class SearchComponent implements OnInit, OnDestroy {
  readonly api = inject(ApiService);
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
  hasFilters     = signal(false);
  selectedFile   = signal<FileInfo | null>(null);
  loadingDetails = signal(false);

  readonly PAGE_SIZE = 50;

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

  ngOnInit(): void {
    this.api.getAllKeywords().subscribe(kws => this.allKeywords.set(kws));

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

  onFilterChange(): void {
    const hasAny =
      this.selectedMediaTypes().size > 0 ||
      this.selectedKeywords().length > 0 ||
      !!this.country() ||
      !!this.dateFrom() ||
      !!this.dateTo() ||
      !!this.cameraMake() ||
      !!this.cameraModel() ||
      !!this.videoCodec();
    this.hasFilters.set(hasAny);
    if (hasAny) this.filterChange$.next();
    else { this.results.set([]); this.total.set(0); }
  }

  clearFacet(field: 'country' | 'cameraMake' | 'cameraModel' | 'videoCodec'): void {
    if (field === 'country')     this.country.set('');
    if (field === 'cameraMake')  this.cameraMake.set('');
    if (field === 'cameraModel') this.cameraModel.set('');
    if (field === 'videoCodec')  this.videoCodec.set('');
    this.onFilterChange();
  }

  loadMore(): void {
    this.currentPage.set(this.currentPage() + 1);
    this.runSearch(true);
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
      page,
      page_size:   this.PAGE_SIZE,
    };
  }

  private runSearch(append: boolean): void {
    this.loading.set(true);
    this.api.searchFiles(this.buildQuery(this.currentPage())).subscribe({
      next: (resp: SearchResponse) => {
        this.total.set(resp.total);
        this.results.set(append ? [...this.results(), ...resp.items] : resp.items);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  selectResult(result: SearchResult): void {
    const path = result.directory + '/' + result.file_name;
    this.selectedFile.set(null);
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
}
