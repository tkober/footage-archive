import { Injectable, signal } from '@angular/core';

/**
 * Cache-busting for clip-preview URLs (#64). `/files/clip-preview/{md5}` is a
 * stable URL per hash, so the browser (and the media-card's `imgError`
 * signal) would keep showing the old/missing image after a rescan silently
 * regenerates it. `ApiService.clipPreviewUrl()` appends `?v=<version>` for
 * any hash bumped here, which both busts the HTTP cache and changes the
 * `previewUrl` input the media-card resets `imgError` on.
 */
@Injectable({ providedIn: 'root' })
export class PreviewCacheService {
  private versions = signal<ReadonlyMap<string, number>>(new Map());

  versionFor(md5Hash: string): number | undefined {
    return this.versions().get(md5Hash);
  }

  /** Mark these hashes' previews as freshly regenerated — called once a
      rescan task (POST /tracking/refresh) reaches COMPLETED/FAILED. */
  bump(md5Hashes: Iterable<string>): void {
    const next = new Map(this.versions());
    const v = Date.now();
    for (const hash of md5Hashes) next.set(hash, v);
    this.versions.set(next);
  }
}
