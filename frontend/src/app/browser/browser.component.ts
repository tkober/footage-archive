import { DatePipe } from '@angular/common';
import { Component, computed, effect, ElementRef, HostListener, inject, OnInit, signal, ViewChild } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { forkJoin, switchMap, map, tap } from 'rxjs';
import * as L from 'leaflet';

import { ContextMenuComponent } from './context-menu/context-menu.component';
import { ModalComponent } from '../modal/modal.component';
import { ApiService } from '../services/api.service';
import { FileInfo, Location, PathChild, VIDEO_TYPES, PHOTO_TYPES } from '../models';

const PAGE_SIZE = 50;

@Component({
  selector: 'app-browser',
  standalone: true,
  imports: [DatePipe, ContextMenuComponent, ModalComponent],
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
  geocoding     = signal(false);
  geocodeError  = signal(false);

  bulkMode       = signal(false);
  bulkSelected   = signal<Set<string>>(new Set());
  bulkKeyword    = signal('');
  bulkLocationId = signal('');
  bulkApplying   = signal(false);
  keywordSuggestions = computed(() => {
    const input = this.newKeywordValue().toLowerCase();
    const applied = new Set(this.selectedFile()?.keywords ?? []);
    return this.allKeywords().filter(
      kw => !applied.has(kw) && (input === '' || kw.toLowerCase().includes(input))
    );
  });
  @ViewChild('nameInput') nameInputRef?: ElementRef<HTMLInputElement>;
  @ViewChild('locMapContainer') locMapContainerRef?: ElementRef<HTMLDivElement>;
  private locMap: L.Map | null = null;
  private locMarker: L.Marker | null = null;

  @ViewChild('detailMapContainer') detailMapContainerRef?: ElementRef<HTMLDivElement>;
  private detailMap: L.Map | null = null;

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

  constructor() {
    effect(() => {
      if (this.showCreateLocation()) {
        setTimeout(() => this.initLocMap(), 0);
      } else {
        this.destroyLocMap();
      }
    });

    effect(() => {
      const loc = this.selectedFile()?.location;
      const lat = loc?.latitude;
      const lon = loc?.longitude;
      if (lat != null && lon != null) {
        setTimeout(() => this.initDetailMap(lat, lon), 0);
      } else {
        this.destroyDetailMap();
      }
    });
  }

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
          this.api.getAllKeywords().subscribe({ next: kws => this.allKeywords.set(kws) });
          this.api.getLocations().subscribe({ next: locs => this.allLocations.set(locs) });
        },
        error: () => this.loadingDetails.set(false),
      });
    }
  }

  @HostListener('document:keydown.escape')
  onEscapeKey() {
    if (this.showCreateLocation()) return; // modal handles its own ESC
    if (this.showDetail()) this.closeDetails();
    else if (this.bulkMode()) this.exitBulkMode();
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
        if (this.bulkMode()) {
          this.bulkAssignLocation(loc.id);
        } else {
          this.assignLocation(loc.id);
        }
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
