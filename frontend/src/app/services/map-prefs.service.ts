import { Injectable, signal } from '@angular/core';

const STORAGE_KEY = 'fa-map-places';

/**
 * Persists the Settings-page default for the map "Places" toggle (#107) —
 * whether sights/parks/train stations show on maps (via the second,
 * POI-style Map ID, `GOOGLE_MAPS_MAP_ID_POI`). This only stores the
 * *default*: the Map page reads it once per visit to seed its own
 * session-only toggle and never writes back, so a mid-session flip there
 * doesn't silently change what Settings shows.
 */
@Injectable({ providedIn: 'root' })
export class MapPrefsService {
  readonly showPlaces = signal<boolean>(this.readStored());

  setShowPlaces(value: boolean): void {
    this.showPlaces.set(value);
    try {
      localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
    } catch {
      /* storage unavailable (private mode, quota, ...) — still applies this session */
    }
  }

  private readStored(): boolean {
    try {
      return localStorage.getItem(STORAGE_KEY) === '1';
    } catch {
      /* storage unavailable — fall through to default */
    }
    return false;
  }
}
