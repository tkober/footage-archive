import { Component, HostListener, OnDestroy, OnInit, computed, inject, signal, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { EMPTY, Subject, Subscription } from 'rxjs';
import { catchError, debounceTime, switchMap } from 'rxjs/operators';
import { GoogleMap, MapAdvancedMarker } from '@angular/google-maps';

import { ApiService } from '../services/api.service';
import { GoogleMapsLoaderService } from '../services/google-maps-loader.service';
import { MapPrefsService } from '../services/map-prefs.service';
import { ThemeService } from '../services/theme.service';
import { IconComponent } from '../shared/icon/icon.component';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { MapPreviewPanelComponent } from './map-preview-panel/map-preview-panel.component';
import { FileInfo, MapMember, MapPoint } from '../models';

type MarkerKind = 'single' | 'strip' | 'cluster';

interface RenderedMarker {
  key: string;
  position: google.maps.LatLngLiteral;
  content: HTMLElement;
  point: MapPoint;
  kind: MarkerKind;
}

// White marker glyphs (Material Symbols paths) shown on a missing-preview tile.
const ICON_IMAGE = '<svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>';
const ICON_FILM  = '<svg viewBox="0 0 24 24"><path d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/></svg>';
const ICON_360   = '<svg viewBox="0 0 24 24"><path d="M12 7C6.48 7 2 9.24 2 12c0 2.24 2.94 4.13 7 4.77V20l4-4-4-4v2.73c-3.15-.56-5-1.9-5-2.73 0-1.06 3.04-3 8-3s8 1.94 8 3c0 .73-1.46 1.89-4 2.53v2.05c3.53-.77 6-2.53 6-4.58 0-2.76-4.48-5-10-5z"/></svg>';

@Component({
  selector: 'app-map',
  standalone: true,
  imports: [GoogleMap, MapAdvancedMarker, FileDetailPanelComponent, MapPreviewPanelComponent, IconComponent],
  templateUrl: './map.component.html',
  styleUrl: './map.component.css',
  host: { class: 'page-flush' },
})
export class MapComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private loader = inject(GoogleMapsLoaderService);
  private router = inject(Router);
  private theme = inject(ThemeService);
  private mapPrefs = inject(MapPrefsService);

  readonly map = viewChild(GoogleMap);
  readonly previewPanel = viewChild(MapPreviewPanelComponent);

  mapsReady = signal(false);
  mapsDisabled = signal(false);
  mapId = signal('');
  mapIdPoi = signal('');
  /** "Places" toggle (#107) — seeded from the Settings-page default on each
      visit to this page, then session-only: it is never written back to
      `MapPrefsService`, so flipping it here doesn't change what Settings
      shows next time. */
  placesOn = signal(false);
  /** Effective Map ID: the POI-style one while the toggle is on and a
      second Map ID is actually configured, else the quiet default. */
  readonly effectiveMapId = computed(() => this.placesOn() && this.mapIdPoi() ? this.mapIdPoi() : this.mapId());
  markers = signal<RenderedMarker[]>([]);
  /** The point backing the currently-open preview panel (#106). Kept as the
      exact `MapPoint` object the user clicked on, so the panel survives a
      marker refetch (pan/zoom) that may replace the markers array —
      nothing re-renders it unless the user closes it or clicks another
      marker. */
  previewPoint = signal<MapPoint | null>(null);
  /** Key of the marker whose preview panel is open (#105/#106) — drives the
      selected ring via `applySelectionRing`, cleared on close/re-render/
      another click. */
  private selectedKey = signal<string | null>(null);

  /** Sum of `count` over the currently rendered markers — shown in the
      "Search this area · N" pill without an extra request. */
  readonly totalMarkerCount = computed(() => this.markers().reduce((sum, m) => sum + m.point.count, 0));

  /** True while a `/locations/map-points` request is in flight — drives the
      thin loading bar at the top of the map (#104). */
  loading = signal(false);
  /** True once the first response (success or empty) has been rendered, so
      the empty-state chip never flashes before the initial fetch resolves. */
  private loadedOnce = signal(false);
  /** Empty state: the last response had 0 points and nothing is in flight. */
  readonly showEmptyState = computed(() =>
    this.loadedOnce() && !this.loading() && this.markers().length === 0);

  /** Google basemap type (roadmap/satellite), controlled by the segmented
      control (#104). Stored in a signal — rather than mutated directly on
      `googleMap` only — so a #102 theme-triggered map rebuild (`mapKey`)
      recreates the map with the same choice instead of resetting to roadmap. */
  mapTypeId = signal<'roadmap' | 'hybrid'>('roadmap');

  // Embedded detail panel (slides in like the Browser/Search views)
  selectedFile = signal<FileInfo | null>(null);
  loadingDetails = signal(false);

  readonly initialCenter: google.maps.LatLngLiteral = { lat: 20, lng: 0 };
  readonly initialZoom = 2;
  /** Current viewport, kept in sync on every `(idle)` so a theme-triggered
      map rebuild (see `mapKey`) re-opens at the same place instead of
      resetting to the world view. `savedCenter`'s object identity is never
      replaced (its lat/lng are mutated in place) — @angular/google-maps
      calls `setCenter()` again whenever it sees a *new* `center` object, so
      reassigning it on every `idle` would fight the user's own pan/zoom and
      retrigger `idle` in a feedback loop. `savedZoom` is a primitive, so
      Angular only re-applies it when the number actually changes. */
  savedCenter: google.maps.LatLngLiteral = { ...this.initialCenter };
  savedZoom = this.initialZoom;

  /** Options depend on the resolved theme so switching it (Settings, or a
      system theme change while on "System") is picked up live — see `mapKey`. */
  readonly mapOptions = computed<google.maps.MapOptions>(() => ({
    colorScheme: this.theme.resolved() === 'light' ? 'LIGHT' : 'DARK',
    mapTypeId: this.mapTypeId(),
    // All of Google's own chrome is replaced by our own styled overlays
    // (#104) — only the legal/attribution footer stays, it can't be removed.
    streetViewControl: false,
    fullscreenControl: false,
    mapTypeControl: false,
    zoomControl: false,
    cameraControl: false,
    clickableIcons: false,
  }));

  /** `@for` track key for the `<google-map>`: changing it destroys and
      recreates the map, which is the only way to apply a new colorScheme or
      Map ID (Google doesn't support switching either on a live map).
      `placesOn` is folded in explicitly (not just the resulting
      `effectiveMapId`) so a toggle always forces a rebuild even in the
      degenerate case where `GOOGLE_MAPS_MAP_ID_POI` happens to equal
      `GOOGLE_MAPS_MAP_ID`. */
  readonly mapKey = computed(() => `${this.theme.resolved()}|${this.effectiveMapId()}|${this.placesOn()}`);

  private reload$ = new Subject<void>();
  private sub?: Subscription;

  async ngOnInit(): Promise<void> {
    const ok = await this.loader.load();
    if (!ok) {
      this.mapsDisabled.set(true);
      return;
    }
    this.mapId.set(this.loader.mapId);
    this.mapIdPoi.set(this.loader.mapIdPoi);
    this.placesOn.set(this.mapPrefs.showPlaces());

    this.sub = this.reload$.pipe(
      debounceTime(300),
      switchMap(() => {
        const bounds = this.map()?.getBounds();
        // getZoom() can be fractional (trackpad/scroll); the API wants an int.
        const zoom = Math.round(this.map()?.getZoom() ?? 2);
        if (!bounds) return EMPTY;
        const ne = bounds.getNorthEast();
        const sw = bounds.getSouthWest();
        this.loading.set(true);
        return this.api.getMapPoints(
          { west: sw.lng(), south: sw.lat(), east: ne.lng(), north: ne.lat() },
          zoom,
          // Keep existing markers + the stream alive on a transient failure.
        ).pipe(catchError(() => { this.loading.set(false); return EMPTY; }));
      }),
    ).subscribe(points => {
      this.loading.set(false);
      this.loadedOnce.set(true);
      this.renderPoints(points as MapPoint[]);
    });

    this.mapsReady.set(true);
  }

  /** Refetch markers whenever the view settles (after pan/zoom) and on first render. */
  refresh(): void {
    this.reload$.next();
  }

  /** `(idle)` handler: snapshot the viewport (so a theme-triggered rebuild
      reopens at the same place) then refetch markers as before. */
  onIdle(): void {
    const googleMap = this.map()?.googleMap;
    const c = googleMap?.getCenter();
    if (c) {
      // Mutate in place — see the comment on `savedCenter` for why.
      this.savedCenter.lat = c.lat();
      this.savedCenter.lng = c.lng();
    }
    const z = googleMap?.getZoom();
    if (z != null) this.savedZoom = z;
    this.refresh();
  }

  onMarkerClick(marker: RenderedMarker): void {
    this.previewPoint.set(marker.point);
    this.selectedKey.set(marker.key);
    this.applySelectionRing();
    // Content replaces in place (the panel isn't destroyed/recreated), so
    // explicitly refocus the close button rather than relying on an
    // init-only lifecycle hook — see the component's own comment.
    queueMicrotask(() => this.previewPanel()?.focusClose());
  }

  /** Close via ×, Escape, or a click on the empty map — the selected ring
      belongs to the open panel, so it's cleared whenever the panel closes,
      however it closes. */
  closePreview(): void {
    if (this.previewPoint() === null) return;
    this.previewPoint.set(null);
    this.selectedKey.set(null);
    this.applySelectionRing();
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.closePreview();
  }

  /** `(mapClick)` on `<google-map>` — clicking the empty map (not a marker,
      which stops its own click from bubbling here) closes the panel. */
  onMapClick(): void {
    this.closePreview();
  }

  /** Marker options shared by every marker (#105): bigger clusters win over
      smaller ones under the same point via `[zIndex]`, and — on a vector map
      only, a no-op elsewhere — colliding markers are forced to hide the
      lower-priority one instead of just overlapping. */
  markerOptions(): google.maps.marker.AdvancedMarkerElementOptions {
    return { collisionBehavior: google.maps.CollisionBehavior.REQUIRED_AND_HIDES_OPTIONAL };
  }

  /** Toggle the selected-ring class on every marker's content element so it
      tracks `selectedKey` through clicks, closes and re-renders alike. */
  private applySelectionRing(): void {
    const key = this.selectedKey();
    for (const m of this.markers()) {
      m.content.classList.toggle('marker--selected', m.key === key);
    }
  }

  // ── Preview panel actions (#106) ──

  /** "Zoom in" footer button — fits the map to the open point's bbox. A
      larger left padding on desktop keeps the fitted area clear of the
      panel itself (300px wide + 12px margins ≈ 312px, +40px breathing room). */
  zoomToPreview(): void {
    const p = this.previewPoint();
    const map = this.map()?.googleMap;
    if (!p || !map) return;
    if (p.bbox_west == null || p.bbox_south == null || p.bbox_east == null || p.bbox_north == null) return;
    const bounds = new google.maps.LatLngBounds(
      { lat: p.bbox_south, lng: p.bbox_west },
      { lat: p.bbox_north, lng: p.bbox_east },
    );
    const mobile = window.innerWidth <= 680;
    map.fitBounds(bounds, mobile
      ? { top: 60, right: 60, bottom: 60, left: 60 }
      : { top: 60, right: 60, bottom: 60, left: 352 });
  }

  openInSearch(p: MapPoint): void {
    this.closePreview();
    this.navigateToSearch(p.bbox_west, p.bbox_south, p.bbox_east, p.bbox_north);
  }

  /** "Search this area" button — open search filtered to the current viewport. */
  searchThisArea(): void {
    const bounds = this.map()?.getBounds();
    if (!bounds) return;
    const ne = bounds.getNorthEast();
    const sw = bounds.getSouthWest();
    this.navigateToSearch(sw.lng(), sw.lat(), ne.lng(), ne.lat());
  }

  /** Map/Satellite segmented control (#104) — switched live via `setMapTypeId`
      (no map rebuild needed), and mirrored into `mapTypeId` so a theme-
      triggered rebuild (#102's `mapKey`) re-creates the map with the same type. */
  setMapType(kind: 'roadmap' | 'hybrid'): void {
    this.mapTypeId.set(kind);
    this.map()?.googleMap?.setMapTypeId(kind);
  }

  /** "Places" toggle (#107) — a Map ID can't change on a live map, so
      flipping it rebuilds the map via `mapKey`, same as the #102 theme
      switch; `savedCenter`/`savedZoom`, `mapTypeId` and the open preview
      panel all already survive that rebuild. Session-only — never written
      back to `MapPrefsService`. */
  togglePlaces(): void {
    this.placesOn.update(v => !v);
  }

  /** Zoom +/− buttons (#104), replacing Google's own zoom control. */
  zoomByOne(delta: 1 | -1): void {
    const map = this.map()?.googleMap;
    const current = map?.getZoom() ?? this.savedZoom;
    map?.setZoom(current + delta);
  }

  private navigateToSearch(west: number | null, south: number | null,
                           east: number | null, north: number | null): void {
    this.router.navigate(['/search'], {
      queryParams: { bbox_west: west, bbox_south: south, bbox_east: east, bbox_north: north },
    });
  }

  openDetailsMember(m: MapMember): void {
    this.openDetailsPath(`${m.directory}/${m.file_name}`);
  }

  private openDetailsPath(path: string): void {
    this.closePreview();
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

  /** The panel's own "Move to trash" deleted the open file (#61). Markers are
      clustered server-side, so there's no single marker to patch locally —
      just close the panel and refetch for the current view. */
  onFileDeleted(): void {
    this.closeDetail();
    this.refresh();
  }

  // ── Display helpers ──

  /** "188 photos, 26 videos" (omits a zero part; singular below 2). Used by
      the marker hover tooltip (#105) — the preview panel (#106) builds its
      own copy of this from the `MapPoint` it's given. */
  countsLabel(p: MapPoint): string {
    const parts: string[] = [];
    if (p.photo_count > 0) parts.push(this.pluralize(p.photo_count, 'photo'));
    if (p.video_count > 0) parts.push(this.pluralize(p.video_count, 'video'));
    return parts.join(', ');
  }

  private pluralize(n: number, word: string): string {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  // ── Marker building (#105) ──
  //
  // One marker family, amber (`--accent`) only: a single photo tile (count 1),
  // a small stack of tiles with a count badge (2–3), or a number circle sized
  // by magnitude (4+). All three share a root `.marker` element that carries
  // the hover-tooltip/selected-ring/focus behaviour (map.component.css) so the
  // kind-specific bit below only has to build the visual itself.

  private renderPoints(points: MapPoint[]): void {
    const newMarkers = points.map(p => {
      let kind: MarkerKind;
      let content: HTMLElement;
      if (p.count === 1) {
        kind = 'single';
        content = this.buildSingleContent(p);
      } else if (p.count <= 3) {
        kind = 'strip';
        content = this.buildStackContent(p);
      } else {
        kind = 'cluster';
        content = this.buildClusterContent(p);
      }
      // Keyed by position (not kind) so a point crossing the 1/3/4 count
      // thresholds between refetches updates the existing marker's content
      // in place instead of destroying/recreating it (less flicker). A
      // single file is keyed by its own hash so it keeps its identity even
      // if the server's grid rounding nudges its reported position.
      const key = p.count === 1
        ? (p.md5_hash ?? `single:${p.latitude},${p.longitude}`)
        : `${p.latitude},${p.longitude}`;
      return { key, position: { lat: p.latitude, lng: p.longitude }, content, point: p, kind };
    });
    this.markers.set(newMarkers);
    // The selected marker may no longer exist after a refetch (panned away,
    // or its count/position moved it to a new key) — drop a stale selection
    // instead of leaving a ring on a marker that no longer represents it.
    if (this.selectedKey() !== null && !newMarkers.some(m => m.key === this.selectedKey())) {
      this.selectedKey.set(null);
    }
    this.applySelectionRing();
  }

  /** count 1 → a single 38×38 photo tile, no number. */
  private buildSingleContent(p: MapPoint): HTMLElement {
    const el = this.createMarkerRoot(p, 'single');
    const tile = this.buildTile(p.md5_hash, p.media_type);
    tile.classList.add('marker-tile--front');
    el.appendChild(tile);
    this.appendTooltip(el, p);
    return el;
  }

  /** count 2–3 → a stack of per-file tiles (newest/front on top) plus a
      count badge at the corner. */
  private buildStackContent(p: MapPoint): HTMLElement {
    const el = this.createMarkerRoot(p, 'strip');
    const stack = document.createElement('div');
    stack.className = 'marker-stack';
    const members = (p.members ?? []).slice(0, p.count);
    // Append back-to-front so the front tile (members[0]) paints last and
    // sits visually on top without needing an explicit z-index.
    for (let i = members.length - 1; i >= 0; i--) {
      const m = members[i];
      const tile = this.buildTile(m.md5_hash, m.media_type);
      tile.classList.add('marker-tile--stack', `marker-tile--stack-${i}`);
      if (i === 0) tile.classList.add('marker-tile--front');
      stack.appendChild(tile);
    }
    const badge = document.createElement('div');
    badge.className = 'marker-count-badge';
    badge.textContent = String(p.count);
    stack.appendChild(badge);
    el.appendChild(stack);
    this.appendTooltip(el, p);
    return el;
  }

  /** count ≥ 4 → a number circle, sized by magnitude. */
  private buildClusterContent(p: MapPoint): HTMLElement {
    const el = this.createMarkerRoot(p, 'cluster');
    const circle = document.createElement('div');
    circle.className = `marker-circle marker-circle--${this.clusterSize(p.count)}`;
    circle.textContent = this.formatClusterCount(p.count);
    el.appendChild(circle);
    this.appendTooltip(el, p);
    return el;
  }

  /** Root element shared by every marker kind: accessible title, focusable
      (the advanced marker itself takes keyboard focus because it has a click
      listener — the `:focus-visible` outline lives in the CSS), and the
      hover-scale/selected-ring hook via `marker--<kind>`. */
  private createMarkerRoot(p: MapPoint, kind: MarkerKind): HTMLElement {
    const el = document.createElement('div');
    el.className = `marker marker--${kind}`;
    el.title = p.place ? `${this.pluralize(p.count, 'file')} in ${p.place}` : this.pluralize(p.count, 'file');
    return el;
  }

  /** Hover tooltip: "214 files · 188 photos, 26 videos" — the "· …" part is
      its own span so the CSS can mute it independently. Hidden entirely
      while the marker is selected (see `.marker--selected .marker-tooltip`). */
  private appendTooltip(el: HTMLElement, p: MapPoint): void {
    const tooltip = document.createElement('div');
    tooltip.className = 'marker-tooltip';
    const main = document.createElement('span');
    main.textContent = this.pluralize(p.count, 'file');
    tooltip.appendChild(main);
    const sub = this.countsLabel(p);
    if (sub) {
      const subEl = document.createElement('span');
      subEl.className = 'marker-tooltip-sub';
      subEl.textContent = ` · ${sub}`;
      tooltip.appendChild(subEl);
    }
    el.appendChild(tooltip);
  }

  /** One file's tile: its preview image, or — on a 404 (preview not yet
      generated) — a neutral tile with a white media-type glyph. */
  private buildTile(md5: string | null, mediaType: string | null): HTMLElement {
    const tile = document.createElement('div');
    tile.className = 'marker-tile';
    if (md5) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = this.api.clipPreviewUrl(md5);
      img.addEventListener('error', () => {
        img.remove();
        tile.classList.add('marker-tile--fallback');
        tile.innerHTML = this.iconFor(mediaType);
      }, { once: true });
      tile.appendChild(img);
    } else {
      tile.classList.add('marker-tile--fallback');
      tile.innerHTML = this.iconFor(mediaType);
    }
    return tile;
  }

  private iconFor(mediaType: string | null): string {
    if (mediaType === '360_video' || mediaType === '360_photo') return ICON_360;
    if (mediaType === 'video') return ICON_FILM;
    return ICON_IMAGE;
  }

  /** 4–9 → 30px, 10–99 → 36px, 100–999 → 42px, ≥1000 → 48px. */
  private clusterSize(count: number): number {
    if (count < 10) return 30;
    if (count < 100) return 36;
    if (count < 1000) return 42;
    return 48;
  }

  /** < 1000 plain; ≥ 1000 as "1.2k"/"12k" (one decimal below 10k, none at or
      above it, trailing ".0" dropped). */
  private formatClusterCount(count: number): string {
    if (count < 1000) return String(count);
    const thousands = count / 1000;
    const decimals = thousands < 10 ? 1 : 0;
    return `${thousands.toFixed(decimals).replace(/\.0$/, '')}k`;
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }
}
