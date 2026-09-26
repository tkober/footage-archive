import { Component, computed, effect, ElementRef, inject, OnDestroy, signal, untracked, ViewChild, viewChild, input, output } from '@angular/core';
import { DatePipe, JsonPipe } from '@angular/common';
import { GoogleMap, MapAdvancedMarker, MapGeocoder } from '@angular/google-maps';

import { ModalComponent } from '../../modal/modal.component';
import { ImageViewerComponent } from '../image-viewer/image-viewer.component';
import { ApiService } from '../../services/api.service';
import { GoogleMapsLoaderService } from '../../services/google-maps-loader.service';
import { ExifTag, FileInfo, Location, ShotClassification, VIDEO_TYPES, PHOTO_TYPES } from '../../models';

@Component({
  selector: 'app-file-detail-panel',
  standalone: true,
  imports: [DatePipe, JsonPipe, ModalComponent, ImageViewerComponent, GoogleMap, MapAdvancedMarker],
  templateUrl: './file-detail-panel.component.html',
  styleUrl: './file-detail-panel.component.css',
})
export class FileDetailPanelComponent implements OnDestroy {
  private api = inject(ApiService);
  private loader = inject(GoogleMapsLoaderService);
  private geocoder = inject(MapGeocoder);

  // ── Inputs / Outputs ──
  file    = input<FileInfo | null>(null);
  loading = input<boolean>(false);
  navIndex = input<number>(-1);   // position of this file within its navigable siblings
  navCount = input<number>(0);    // total navigable siblings (photos in the directory)
  closed  = output<void>();
  renamed = output<FileInfo>();
  navigate = output<number>();    // emits -1 / +1 to step to the prev / next sibling

  // ── Internal file state (owns its own copy, updated by API calls) ──
  selectedFile = signal<FileInfo | null>(null);

  // ── Rename ──
  editingName   = signal(false);
  editNameValue = signal('');
  renameError   = signal<string | null>(null);

  // ── Keywords ──
  newKeywordValue = signal('');
  allKeywords     = signal<string[]>([]);
  keywordSuggestions = computed(() => {
    const input = this.newKeywordValue().toLowerCase();
    const applied = new Set(this.selectedFile()?.keywords ?? []);
    return this.allKeywords().filter(
      kw => !applied.has(kw) && (input === '' || kw.toLowerCase().includes(input))
    );
  });

  // ── Location ──
  allLocations       = signal<Location[]>([]);
  showCreateLocation = signal(false);
  newLocCountry      = signal('');
  newLocRegion       = signal('');
  newLocCity         = signal('');
  newLocName         = signal('');
  newLocLat          = signal('');
  newLocLon          = signal('');
  geocoding          = signal(false);
  geocodeError       = signal(false);

  // ── AI Classification ──
  classificationResult = signal<ShotClassification | null>(null);
  classificationError  = signal<string | null>(null);
  classifying          = signal(false);

  // ── All-metadata (exiftool dump) ──
  showExif    = signal(false);
  exifTags    = signal<ExifTag[]>([]);
  exifLoading = signal(false);
  exifError   = signal<string | null>(null);
  exifGroups  = computed(() => {
    const groups: { group: string; items: ExifTag[] }[] = [];
    let current: { group: string; items: ExifTag[] } | null = null;
    for (const t of this.exifTags()) {
      if (!current || current.group !== t.group) {
        current = { group: t.group, items: [] };
        groups.push(current);
      }
      current.items.push(t);
    }
    return groups;
  });

  // ── Preview ──
  previewUrl = computed(() => {
    const file = this.selectedFile();
    if (!file?.md5_hash) return null;
    if (!VIDEO_TYPES.includes(file.media_type as any) && !PHOTO_TYPES.includes(file.media_type as any)) return null;
    return this.api.clipPreviewUrl(file.md5_hash);
  });

  // ── High quality (full-resolution still) ──
  hqUrl      = signal<string | null>(null);   // object URL of the fetched full-res image
  hqFetching = signal(false);
  hqError    = signal(false);
  /** Viewer source: full-res once fetched, otherwise the ~600px preview. */
  viewerUrl  = computed(() => this.hqUrl() ?? this.previewUrl());
  /** Aspect ratio (w/h) of the loaded image, so the frame hugs it (no narrow
      pillarbox). Re-measured on each load, so it adjusts when HQ swaps in. */
  viewerAspect = signal<number | null>(null);

  // ── DOM refs ──
  @ViewChild('nameInput') nameInputRef?: ElementRef<HTMLInputElement>;
  private locMapRef = viewChild<GoogleMap>('locMapRef');

  // ── Maps (Google) ──
  mapsReady = signal(false);
  mapId = signal('');
  readonly detailMapOptions: google.maps.MapOptions = {
    streetViewControl: false, fullscreenControl: false, mapTypeControl: false, clickableIcons: false,
  };
  readonly locMapOptions: google.maps.MapOptions = {
    streetViewControl: false, fullscreenControl: false, mapTypeControl: true, clickableIcons: false,
  };
  /** Read-only detail-map coords: assigned-location coords first, raw GPS fallback. */
  detailCoords = computed<google.maps.LatLngLiteral | null>(() => {
    const f = this.selectedFile();
    const lat = f?.location?.latitude ?? f?.latitude;
    const lon = f?.location?.longitude ?? f?.longitude;
    if (lat == null || lon == null) return null;
    return { lat, lng: lon };
  });
  // Interactive new-location map state
  locCenter = signal<google.maps.LatLngLiteral>({ lat: 20, lng: 0 });
  locZoom = signal(2);
  locMarkerPos = signal<google.maps.LatLngLiteral | null>(null);
  private _detailPin?: HTMLElement;
  private _locPin?: HTMLElement;

  constructor() {
    // Sync input → local state; reset UI when a different file is opened
    effect(() => {
      const f = this.file();
      this.selectedFile.set(f);
      this.editingName.set(false);
      this.renameError.set(null);
      this.newKeywordValue.set('');
      this.classificationResult.set(null);
      this.classificationError.set(null);
      this.classifying.set(false);
      // untracked: resetHq reads hqUrl(), and we must not make this effect
      // depend on it — otherwise fetching HQ would re-trigger the reset.
      untracked(() => this.resetHq());   // drop any full-res image from the previous file
      if (f) {
        this.api.getAllKeywords().subscribe(kws => this.allKeywords.set(kws));
        this.api.getLocations().subscribe(locs => this.allLocations.set(locs));
      }
    });

    // Load the Google Maps JS API once (key + Map ID come from /config). The
    // <google-map> elements stay hidden behind mapsReady() until it resolves.
    // The detail map is declarative (detailCoords); the new-location map is
    // declarative (showCreateLocation + locMarkerPos) — no imperative init.
    this.loader.load().then(ok => {
      if (ok) {
        this.mapsReady.set(true);
        this.mapId.set(this.loader.mapId);
      }
    });
  }

  ngOnDestroy() {
    this.resetHq();
  }

  // ── Actions ──

  close() { this.closed.emit(); }

  // ── High quality ──

  /** Fetch the full-resolution still on demand; the viewer then swaps to it. */
  fetchHighQuality() {
    const file = this.selectedFile();
    if (!file?.md5_hash || this.hqFetching() || this.hqUrl()) return;
    this.hqFetching.set(true);
    this.hqError.set(false);
    this.api.fetchFullImage(file.md5_hash).subscribe({
      next: blob => {
        this.hqUrl.set(URL.createObjectURL(blob));
        this.hqFetching.set(false);
      },
      error: () => { this.hqError.set(true); this.hqFetching.set(false); },
    });
  }

  /** Revoke any cached full-res object URL and clear HQ state. */
  private resetHq() {
    const url = this.hqUrl();
    if (url) URL.revokeObjectURL(url);
    this.hqUrl.set(null);
    this.hqFetching.set(false);
    this.hqError.set(false);
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
    if (!newName || newName === file.name) { this.cancelNameEdit(); return; }
    this.api.renameFile(file.path, newName).subscribe({
      next: (updated) => {
        this.selectedFile.set(updated);
        this.editingName.set(false);
        this.renameError.set(null);
        this.renamed.emit(updated);
      },
      error: (err) => this.renameError.set(err.error?.detail ?? 'Rename failed'),
    });
  }

  cancelNameEdit() {
    this.editingName.set(false);
    this.renameError.set(null);
  }

  private reloadFile() {
    const file = this.selectedFile();
    if (!file) return;
    this.api.getFileDetails(file.path).subscribe({
      next: info => this.selectedFile.set(info),
    });
  }

  classifyShot() {
    const file = this.selectedFile();
    if (!file?.path) return;
    this.classifying.set(true);
    this.classificationResult.set(null);
    this.classificationError.set(null);
    this.api.classifyShot(file.path).subscribe({
      next: r  => { this.classificationResult.set(r); this.classifying.set(false); },
      error: e => { this.classificationError.set(e.error?.detail ?? 'Classification failed'); this.classifying.set(false); },
    });
  }

  openExif() {
    const file = this.selectedFile();
    if (!file?.path) return;
    this.showExif.set(true);
    this.exifLoading.set(true);
    this.exifTags.set([]);
    this.exifError.set(null);
    this.api.getFileExif(file.path).subscribe({
      next: tags => { this.exifTags.set(tags); this.exifLoading.set(false); },
      error: e    => { this.exifError.set(e.error?.detail ?? 'Failed to load metadata'); this.exifLoading.set(false); },
    });
  }

  addKeyword() {
    const file = this.selectedFile();
    const kw = this.newKeywordValue().trim();
    if (!file?.md5_hash || !kw) return;
    this.newKeywordValue.set('');
    this.api.addKeyword(file.md5_hash, kw).subscribe({
      next: () => {
        this.reloadFile();
        this.api.getAllKeywords().subscribe(kws => this.allKeywords.set(kws));
      },
    });
  }

  removeKeyword(keyword: string) {
    const file = this.selectedFile();
    if (!file?.md5_hash) return;
    this.api.removeKeyword(file.md5_hash, keyword).subscribe({
      next: () => this.reloadFile(),
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
      country:   this.newLocCountry() || null,
      region:    this.newLocRegion()  || null,
      city:      this.newLocCity()    || null,
      name:      this.newLocName()    || null,
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
    this.geocoding.set(false);
    this.geocodeError.set(false);
    this.locMarkerPos.set(null);
    this.locCenter.set({ lat: 20, lng: 0 });
    this.locZoom.set(2);
  }

  geocodeLocation() {
    const name    = this.newLocName().trim();
    const city    = this.newLocCity().trim();
    const region  = this.newLocRegion().trim();
    const country = this.newLocCountry().trim();
    if (!name && !city && !country) return;
    if (!this.mapsReady()) { this.geocodeError.set(true); return; }
    const q1 = [name, city, region, country].filter(Boolean).join(', ');
    const q2 = [name, city, country].filter(Boolean).join(', ');
    const q3 = [city, country].filter(Boolean).join(', ');
    const queries = [...new Set([q1, q2, q3].filter(Boolean))];
    this.geocoding.set(true);
    this.geocodeError.set(false);
    this.tryGeocode(queries, 0);
  }

  private tryGeocode(queries: string[], index: number) {
    if (index >= queries.length) {
      this.geocoding.set(false);
      this.geocodeError.set(true);
      return;
    }
    this.geocoder.geocode({ address: queries[index] }).subscribe({
      next: ({ results, status }) => {
        if (status === google.maps.GeocoderStatus.OK && results.length) {
          this.geocoding.set(false);
          const loc = results[0].geometry.location;
          this.setLocPin(loc.lat(), loc.lng());
        } else {
          this.tryGeocode(queries, index + 1);
        }
      },
      error: () => this.tryGeocode(queries, index + 1),
    });
  }

  // ── Maps (Google) ──

  /** Marker DOM for the read-only detail map (a coloured dot). */
  get detailPinContent(): HTMLElement {
    return (this._detailPin ??= this.makeLocPin());
  }

  /** Marker DOM for the draggable new-location pin. */
  get locPinContent(): HTMLElement {
    return (this._locPin ??= this.makeLocPin());
  }

  private makeLocPin(): HTMLElement {
    const el = document.createElement('div');
    el.className = 'loc-map-pin';
    return el;
  }

  /** Click on the new-location map → drop / move the pin there (and recentre). */
  onLocMapClick(event: google.maps.MapMouseEvent | google.maps.IconMouseEvent): void {
    const ll = (event as google.maps.MapMouseEvent).latLng;
    if (ll) this.setLocPin(ll.lat(), ll.lng());
  }

  /** Drag end → update the coordinate fields only (no recentre, matching the old UX). */
  onLocMarkerDragend(marker: MapAdvancedMarker): void {
    const pos = marker.advancedMarker.position;
    if (!pos) return;
    const lat = typeof pos.lat === 'function' ? pos.lat() : pos.lat;
    const lng = typeof pos.lng === 'function' ? pos.lng() : pos.lng;
    if (lat == null || lng == null) return;
    this.newLocLat.set(lat.toFixed(6));
    this.newLocLon.set(lng.toFixed(6));
    this.locMarkerPos.set({ lat, lng });
  }

  private setLocPin(lat: number, lon: number) {
    this.newLocLat.set(lat.toFixed(6));
    this.newLocLon.set(lon.toFixed(6));
    this.locMarkerPos.set({ lat, lng: lon });
    this.locCenter.set({ lat, lng: lon });
    const currentZoom = this.locMapRef()?.getZoom() ?? this.locZoom();
    this.locZoom.set(Math.max(currentZoom, 10));
  }

  // ── Display helpers ──

  isPhotoMediaType(mediaType: string | null | undefined): boolean {
    return PHOTO_TYPES.includes(mediaType as any);
  }

  formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }

  locationGeo(loc: Location): string {
    return [loc.country, loc.region, loc.city].filter(Boolean).join(' › ');
  }

  locationName(loc: Location): string {
    return loc.name ?? this.locationGeo(loc);
  }

  formatLocation(loc: Location): string {
    const geo = this.locationGeo(loc);
    return loc.name ? `${loc.name} — ${geo}` : geo;
  }
}
