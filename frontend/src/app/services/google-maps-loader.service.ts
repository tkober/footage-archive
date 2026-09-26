import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ApiService } from './api.service';

/**
 * Loads the Google Maps JavaScript API exactly once, using the key served by the
 * backend's /config endpoint (so the key lives in the environment, not in git).
 * Components await load() and gate their <google-map> on the result.
 */
@Injectable({ providedIn: 'root' })
export class GoogleMapsLoaderService {
  private api = inject(ApiService);
  private loadPromise: Promise<boolean> | null = null;
  private _mapId = '';

  /** Cloud Map ID required for Advanced Markers (empty when maps are disabled). */
  get mapId(): string {
    return this._mapId;
  }

  /**
   * Resolves true once `google.maps` (incl. the maps + marker libraries) is ready,
   * or false when no API key is configured. Idempotent — repeat callers share the
   * same in-flight promise and never trigger a second script load.
   */
  load(): Promise<boolean> {
    if (!this.loadPromise) {
      this.loadPromise = this.doLoad();
    }
    return this.loadPromise;
  }

  private async doLoad(): Promise<boolean> {
    if (typeof google !== 'undefined' && google.maps?.Map) {
      return true;
    }
    const cfg = await firstValueFrom(this.api.getConfig());
    this._mapId = cfg.google_maps_map_id ?? '';
    const key = cfg.google_maps_api_key;
    if (!key) {
      return false; // maps disabled — components fall back to a placeholder
    }
    await this.injectScript(key);
    // Ensure the libraries our components use are present before we report ready.
    await Promise.all([
      google.maps.importLibrary('maps'),
      google.maps.importLibrary('marker'),
    ]);
    return true;
  }

  private injectScript(key: string): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (document.getElementById('google-maps-js')) {
        resolve();
        return;
      }
      const callbackName = '__footageArchiveGoogleMapsReady';
      (window as any)[callbackName] = () => resolve();
      const script = document.createElement('script');
      script.id = 'google-maps-js';
      script.async = true;
      script.src =
        'https://maps.googleapis.com/maps/api/js' +
        `?key=${encodeURIComponent(key)}` +
        '&v=weekly&loading=async&libraries=marker' +
        `&callback=${callbackName}`;
      script.onerror = () => reject(new Error('Failed to load the Google Maps JavaScript API'));
      document.head.appendChild(script);
    });
  }
}
