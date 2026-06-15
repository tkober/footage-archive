import { Component, OnDestroy, OnInit, inject, signal, viewChild } from '@angular/core';
import { Subject, Subscription } from 'rxjs';
import { debounceTime, switchMap } from 'rxjs/operators';
import { GoogleMap, MapAdvancedMarker, MapInfoWindow } from '@angular/google-maps';

import { ApiService } from '../services/api.service';
import { GoogleMapsLoaderService } from '../services/google-maps-loader.service';
import { MapPoint, VIDEO_TYPES } from '../models';

interface RenderedMarker {
  key: string;
  position: google.maps.LatLngLiteral;
  content: HTMLElement;
  title: string;
  count: number;
  fileName: string;
  label: string;
}

@Component({
  selector: 'app-map',
  standalone: true,
  imports: [GoogleMap, MapAdvancedMarker, MapInfoWindow],
  templateUrl: './map.component.html',
  styleUrl: './map.component.css',
})
export class MapComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private loader = inject(GoogleMapsLoaderService);

  readonly map = viewChild(GoogleMap);
  readonly infoWindow = viewChild(MapInfoWindow);

  mapsReady = signal(false);
  mapsDisabled = signal(false);
  mapId = signal('');
  markers = signal<RenderedMarker[]>([]);
  infoFileName = signal('');
  infoType = signal('');

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
        const zoom = this.map()?.getZoom() ?? 2;
        if (!bounds) return [];
        const ne = bounds.getNorthEast();
        const sw = bounds.getSouthWest();
        return this.api.getMapPoints(
          { west: sw.lng(), south: sw.lat(), east: ne.lng(), north: ne.lat() },
          zoom,
        );
      }),
    ).subscribe(points => this.renderPoints(points as MapPoint[]));

    this.mapsReady.set(true);
  }

  /** Fired by the map on every pan/zoom (and once on first render). */
  onBoundsChanged(): void {
    this.reload$.next();
  }

  onMarkerClick(marker: RenderedMarker, anchor: MapAdvancedMarker): void {
    const map = this.map()?.googleMap;
    if (marker.count > 1) {
      map?.panTo(marker.position);
      map?.setZoom((this.map()?.getZoom() ?? 2) + 3);
    } else {
      this.infoFileName.set(marker.fileName);
      this.infoType.set(marker.label);
      this.infoWindow()?.open(anchor);
    }
  }

  private renderPoints(points: MapPoint[]): void {
    this.markers.set(points.map((p, i) => {
      const position = { lat: p.latitude, lng: p.longitude };
      if (p.count > 1) {
        return {
          key: `c:${p.latitude},${p.longitude}`,
          position,
          content: this.buildClusterContent(p),
          title: `${p.count} files`,
          count: p.count,
          fileName: '',
          label: '',
        };
      }
      const label = p.media_type?.replace('_', ' ') ?? 'unknown';
      return {
        key: p.md5_hash ?? `p:${i}`,
        position,
        content: this.buildPinContent(p),
        title: p.file_name ?? '',
        count: 1,
        fileName: p.file_name ?? '',
        label,
      };
    }));
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
