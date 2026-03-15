import { Component, ElementRef, OnDestroy, AfterViewInit, viewChild, inject } from '@angular/core';
import { Subject, Subscription } from 'rxjs';
import { debounceTime, switchMap } from 'rxjs/operators';
import * as L from 'leaflet';

import { ApiService } from '../services/api.service';
import { MapPoint, VIDEO_TYPES } from '../models';

@Component({
  selector: 'app-map',
  standalone: true,
  templateUrl: './map.component.html',
  styleUrl: './map.component.css',
})
export class MapComponent implements AfterViewInit, OnDestroy {
  private api = inject(ApiService);
  private map: L.Map | null = null;
  private markers = L.layerGroup();
  private reload$ = new Subject<void>();
  private sub: Subscription | null = null;

  readonly mapContainer = viewChild.required<ElementRef>('mapContainer');

  ngAfterViewInit(): void {
    const el = this.mapContainer().nativeElement;
    this.map = L.map(el).setView([20, 0], 2);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(this.map);

    this.markers.addTo(this.map);

    this.sub = this.reload$.pipe(
      debounceTime(300),
      switchMap(() => this.api.getMapPoints(this.map!.getBounds(), this.map!.getZoom())),
    ).subscribe(points => this.renderPoints(points));

    this.map.on('moveend', () => this.reload$.next());
    this.reload$.next();
  }

  private renderPoints(points: MapPoint[]): void {
    this.markers.clearLayers();

    for (const p of points) {
      if (p.count > 1) {
        const colorClass = p.video_count === 0 ? 'mc-badge--photo'
          : p.photo_count === 0 ? 'mc-badge--video'
          : 'mc-badge--mixed';
        const icon = L.divIcon({
          className: '',
          html: `<div class="mc-badge ${colorClass}">${p.count}</div>`,
          iconSize: [36, 36],
          iconAnchor: [18, 18],
        });
        const marker = L.marker([p.latitude, p.longitude], { icon });
        marker.on('click', () => {
          this.map!.setView([p.latitude, p.longitude], this.map!.getZoom() + 3);
        });
        marker.addTo(this.markers);
      } else {
        const isVideo = VIDEO_TYPES.includes(p.media_type as any);
        const icon = L.divIcon({
          className: isVideo ? 'map-pin map-pin-video' : 'map-pin map-pin-photo',
          iconSize: [14, 14],
          iconAnchor: [7, 7],
        });
        const label = p.media_type?.replace('_', ' ') ?? 'unknown';
        L.marker([p.latitude, p.longitude], { icon })
          .bindPopup(`<strong>${p.file_name}</strong><br><span class="map-popup-type">${label}</span>`)
          .addTo(this.markers);
      }
    }
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
    this.map?.remove();
    this.map = null;
  }
}
