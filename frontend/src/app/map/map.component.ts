import { Component, OnDestroy, OnInit, inject, signal, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { EMPTY, Subject, Subscription } from 'rxjs';
import { catchError, debounceTime, switchMap } from 'rxjs/operators';
import { GoogleMap, MapAdvancedMarker, MapInfoWindow } from '@angular/google-maps';

import { ApiService } from '../services/api.service';
import { GoogleMapsLoaderService } from '../services/google-maps-loader.service';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { FileInfo, MapMember, MapPoint, VIDEO_TYPES } from '../models';

type MarkerKind = 'single' | 'strip' | 'cluster';

interface RenderedMarker {
  key: string;
  position: google.maps.LatLngLiteral;
  content: HTMLElement;
  point: MapPoint;
  kind: MarkerKind;
}

// White marker glyphs (Material Symbols paths) shown inside single-file badges.
const ICON_IMAGE = '<svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>';
const ICON_FILM  = '<svg viewBox="0 0 24 24"><path d="M18 4l2 4h-3l-2-4h-2l2 4h-3l-2-4H8l2 4H7L5 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V4h-4z"/></svg>';
const ICON_360   = '<svg viewBox="0 0 24 24"><path d="M12 7C6.48 7 2 9.24 2 12c0 2.24 2.94 4.13 7 4.77V20l4-4-4-4v2.73c-3.15-.56-5-1.9-5-2.73 0-1.06 3.04-3 8-3s8 1.94 8 3c0 .73-1.46 1.89-4 2.53v2.05c3.53-.77 6-2.53 6-4.58 0-2.76-4.48-5-10-5z"/></svg>';

@Component({
  selector: 'app-map',
  standalone: true,
  imports: [GoogleMap, MapAdvancedMarker, MapInfoWindow, FileDetailPanelComponent],
  templateUrl: './map.component.html',
  styleUrl: './map.component.css',
})
export class MapComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private loader = inject(GoogleMapsLoaderService);
  private router = inject(Router);

  readonly map = viewChild(GoogleMap);
  readonly infoWindow = viewChild(MapInfoWindow);

  mapsReady = signal(false);
  mapsDisabled = signal(false);
  mapId = signal('');
  markers = signal<RenderedMarker[]>([]);
  /** The point + kind backing the currently-open info window. */
  infoPoint = signal<MapPoint | null>(null);
  infoKind = signal<MarkerKind>('single');

  // Embedded detail panel (slides in like the Browser/Search views)
  selectedFile = signal<FileInfo | null>(null);
  loadingDetails = signal(false);

  readonly center: google.maps.LatLngLiteral = { lat: 20, lng: 0 };
  readonly zoom = 2;
  readonly mapOptions: google.maps.MapOptions = {
    streetViewControl: false,
    fullscreenControl: false,
    mapTypeControl: true,
    clickableIcons: false,
  };

  private reload$ = new Subject<void>();
  private sub?: Subscription;

  async ngOnInit(): Promise<void> {
    const ok = await this.loader.load();
    if (!ok) {
      this.mapsDisabled.set(true);
      return;
    }
    this.mapId.set(this.loader.mapId);

    this.sub = this.reload$.pipe(
      debounceTime(300),
      switchMap(() => {
        const bounds = this.map()?.getBounds();
        // getZoom() can be fractional (trackpad/scroll); the API wants an int.
        const zoom = Math.round(this.map()?.getZoom() ?? 2);
        if (!bounds) return EMPTY;
        const ne = bounds.getNorthEast();
        const sw = bounds.getSouthWest();
        return this.api.getMapPoints(
          { west: sw.lng(), south: sw.lat(), east: ne.lng(), north: ne.lat() },
          zoom,
          // Keep existing markers + the stream alive on a transient failure.
        ).pipe(catchError(() => EMPTY));
      }),
    ).subscribe(points => this.renderPoints(points as MapPoint[]));

    this.mapsReady.set(true);
  }

  /** Refetch markers whenever the view settles (after pan/zoom) and on first render. */
  refresh(): void {
    this.reload$.next();
  }

  onMarkerClick(marker: RenderedMarker, anchor: MapAdvancedMarker): void {
    this.infoPoint.set(marker.point);
    this.infoKind.set(marker.kind);
    this.infoWindow()?.open(anchor);
  }

  // ── Info-window actions ──

  zoomIn(p: MapPoint): void {
    this.infoWindow()?.close();
    const map = this.map()?.googleMap;
    map?.panTo({ lat: p.latitude, lng: p.longitude });
    map?.setZoom((this.map()?.getZoom() ?? 2) + 3);
  }

  openInSearch(p: MapPoint): void {
    this.infoWindow()?.close();
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

  private navigateToSearch(west: number | null, south: number | null,
                           east: number | null, north: number | null): void {
    this.router.navigate(['/search'], {
      queryParams: { bbox_west: west, bbox_south: south, bbox_east: east, bbox_north: north },
    });
  }

  openDetails(p: MapPoint): void {
    if (p.directory && p.file_name) this.openDetailsPath(`${p.directory}/${p.file_name}`);
  }

  openDetailsMember(m: MapMember): void {
    this.openDetailsPath(`${m.directory}/${m.file_name}`);
  }

  private openDetailsPath(path: string): void {
    this.infoWindow()?.close();
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

  // ── Display helpers ──

  previewUrlFor(p: MapPoint): string | null {
    return p.md5_hash ? this.api.clipPreviewUrl(p.md5_hash) : null;
  }

  memberPreview(m: MapMember): string {
    return this.api.clipPreviewUrl(m.md5_hash);
  }

  mediaLabel(p: MapPoint): string {
    return p.media_type?.replace('_', ' ') ?? 'unknown';
  }

  // ── Marker building ──

  private renderPoints(points: MapPoint[]): void {
    this.markers.set(points.map((p, i) => {
      let kind: MarkerKind;
      let content: HTMLElement;
      if (p.count === 1) {
        kind = 'single';
        content = this.buildSingleContent(p);
      } else if (p.count < 5 && p.video_count === 0 && p.members?.length) {
        kind = 'strip';
        content = this.buildStripContent(p);
      } else {
        kind = 'cluster';
        content = this.buildClusterContent(p);
      }
      const key = kind === 'single'
        ? (p.md5_hash ?? `p:${i}`)
        : `${kind}:${p.latitude},${p.longitude}`;
      return { key, position: { lat: p.latitude, lng: p.longitude }, content, point: p, kind };
    }));
  }

  /** Single file → cluster-sized badge with a type glyph (easy to spot). */
  private buildSingleContent(p: MapPoint): HTMLElement {
    const isVideo = VIDEO_TYPES.includes(p.media_type as any);
    const el = document.createElement('div');
    el.className = `leaf-badge ${isVideo ? 'leaf-badge--video' : 'leaf-badge--still'}`;
    el.innerHTML = this.iconFor(p.media_type);
    return el;
  }

  private iconFor(mediaType: string | null): string {
    if (mediaType === '360_video' || mediaType === '360_photo') return ICON_360;
    if (mediaType === 'video') return ICON_FILM;
    return ICON_IMAGE;
  }

  /** Small all-stills leaf → a contact-sheet strip of the actual thumbnails. */
  private buildStripContent(p: MapPoint): HTMLElement {
    const el = document.createElement('div');
    el.className = 'leaf-strip';
    for (const m of (p.members ?? []).slice(0, 4)) {
      const img = document.createElement('img');
      img.className = 'leaf-strip-thumb';
      img.loading = 'lazy';
      img.src = this.api.clipPreviewUrl(m.md5_hash);
      img.alt = '';
      el.appendChild(img);
    }
    return el;
  }

  private buildClusterContent(p: MapPoint): HTMLElement {
    const colorClass = p.video_count === 0 ? 'mc-badge--photo'
      : p.photo_count === 0 ? 'mc-badge--video'
      : 'mc-badge--mixed';
    const el = document.createElement('div');
    el.className = `mc-badge ${colorClass}`;
    el.textContent = String(p.count);
    return el;
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }
}
