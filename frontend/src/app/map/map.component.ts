import { Component, OnDestroy, OnInit, inject, signal, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { EMPTY, Subject, Subscription } from 'rxjs';
import { catchError, debounceTime, switchMap } from 'rxjs/operators';
import { GoogleMap, MapAdvancedMarker, MapInfoWindow } from '@angular/google-maps';

import { ApiService } from '../services/api.service';
import { GoogleMapsLoaderService } from '../services/google-maps-loader.service';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { FileInfo, MapPoint, VIDEO_TYPES } from '../models';

interface RenderedMarker {
  key: string;
  position: google.maps.LatLngLiteral;
  content: HTMLElement;
  point: MapPoint;
}

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
  /** The point backing the currently-open info window (single file or cluster). */
  infoPoint = signal<MapPoint | null>(null);

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
    this.router.navigate(['/search'], {
      queryParams: {
        bbox_west: p.bbox_west,
        bbox_south: p.bbox_south,
        bbox_east: p.bbox_east,
        bbox_north: p.bbox_north,
      },
    });
  }

  openDetails(p: MapPoint): void {
    if (!p.directory || !p.file_name) return;
    this.infoWindow()?.close();
    const path = `${p.directory}/${p.file_name}`;
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

  mediaLabel(p: MapPoint): string {
    return p.media_type?.replace('_', ' ') ?? 'unknown';
  }

  private renderPoints(points: MapPoint[]): void {
    this.markers.set(points.map((p, i) => ({
      key: p.count > 1 ? `c:${p.latitude},${p.longitude}` : (p.md5_hash ?? `p:${i}`),
      position: { lat: p.latitude, lng: p.longitude },
      content: p.count > 1 ? this.buildClusterContent(p) : this.buildPinContent(p),
      point: p,
    })));
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

  private buildPinContent(p: MapPoint): HTMLElement {
    const isVideo = VIDEO_TYPES.includes(p.media_type as any);
    const el = document.createElement('div');
    el.className = isVideo ? 'map-pin map-pin-video' : 'map-pin map-pin-photo';
    return el;
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }
}
