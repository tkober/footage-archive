import { Component, computed, effect, ElementRef, inject, OnDestroy, signal, untracked, ViewChild, input, output } from '@angular/core';
import { DatePipe, JsonPipe } from '@angular/common';
import * as L from 'leaflet';

import { ModalComponent } from '../../modal/modal.component';
import { ImageViewerComponent } from '../image-viewer/image-viewer.component';
import { ApiService } from '../../services/api.service';
import { ExifTag, FileInfo, Location, ShotClassification, VIDEO_TYPES, PHOTO_TYPES } from '../../models';

@Component({
  selector: 'app-file-detail-panel',
  standalone: true,
  imports: [DatePipe, JsonPipe, ModalComponent, ImageViewerComponent],
  templateUrl: './file-detail-panel.component.html',
  styleUrl: './file-detail-panel.component.css',
})
export class FileDetailPanelComponent implements OnDestroy {
  private api = inject(ApiService);

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

  // ── DOM refs ──
  @ViewChild('nameInput') nameInputRef?: ElementRef<HTMLInputElement>;
  @ViewChild('locMapContainer') locMapContainerRef?: ElementRef<HTMLDivElement>;
  @ViewChild('detailMapContainer') detailMapContainerRef?: ElementRef<HTMLDivElement>;

  private detailMap: L.Map | null = null;
  private locMap: L.Map | null = null;
  private locMarker: L.Marker | null = null;

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

    // Detail map: re-init whenever location/GPS coords change
    effect(() => {
      const file = this.selectedFile();
      const lat = file?.location?.latitude ?? file?.latitude;
      const lon = file?.location?.longitude ?? file?.longitude;
      if (lat != null && lon != null) {
        setTimeout(() => this.initDetailMap(lat, lon), 0);
      } else {
        this.destroyDetailMap();
      }
    });

    // Location creation map
    effect(() => {
      if (this.showCreateLocation()) {
        setTimeout(() => this.initLocMap(), 0);
      } else {
        this.destroyLocMap();
      }
    });
  }

  ngOnDestroy() {
    this.destroyDetailMap();
    this.destroyLocMap();
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
  }

  geocodeLocation() {
    const name    = this.newLocName().trim();
    const city    = this.newLocCity().trim();
    const region  = this.newLocRegion().trim();
    const country = this.newLocCountry().trim();
    if (!name && !city && !country) return;
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
    this.api.geocode(queries[index]).subscribe({
      next: (results) => {
        if (results?.length) {
          this.geocoding.set(false);
          this.setLocPin(parseFloat(results[0].lat), parseFloat(results[0].lon));
        } else {
          this.tryGeocode(queries, index + 1);
        }
      },
      error: () => this.tryGeocode(queries, index + 1),
    });
  }

  // ── Maps ──

  private initDetailMap(lat: number, lon: number) {
    const el = this.detailMapContainerRef?.nativeElement;
    if (!el) return;
    this.destroyDetailMap();
    this.detailMap = L.map(el).setView([lat, lon], 13);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }).addTo(this.detailMap);
    const icon = L.divIcon({ className: 'loc-map-pin', iconSize: [16, 16], iconAnchor: [8, 8] });
    L.marker([lat, lon], { icon }).addTo(this.detailMap);
  }

  private destroyDetailMap() {
    this.detailMap?.remove();
    this.detailMap = null;
  }

  private initLocMap() {
    const el = this.locMapContainerRef?.nativeElement;
    if (!el || this.locMap) return;
    this.locMap = L.map(el).setView([20, 0], 2);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }).addTo(this.locMap);
    this.locMap.on('click', (e: L.LeafletMouseEvent) => this.setLocPin(e.latlng.lat, e.latlng.lng));
    const lat = parseFloat(this.newLocLat());
    const lon = parseFloat(this.newLocLon());
    if (!isNaN(lat) && !isNaN(lon)) this.setLocPin(lat, lon);
  }

  private destroyLocMap() {
    this.locMap?.remove();
    this.locMap = null;
    this.locMarker = null;
  }

  private setLocPin(lat: number, lon: number) {
    this.newLocLat.set(lat.toFixed(6));
    this.newLocLon.set(lon.toFixed(6));
    if (this.locMarker) {
      this.locMarker.setLatLng([lat, lon]);
    } else {
      const icon = L.divIcon({ className: 'loc-map-pin', iconSize: [16, 16], iconAnchor: [8, 8] });
      this.locMarker = L.marker([lat, lon], { draggable: true, icon }).addTo(this.locMap!);
      this.locMarker.on('dragend', () => {
        const pos = this.locMarker!.getLatLng();
        this.newLocLat.set(pos.lat.toFixed(6));
        this.newLocLon.set(pos.lng.toFixed(6));
      });
    }
    this.locMap!.setView([lat, lon], Math.max(this.locMap!.getZoom(), 10));
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
