import { Injectable, inject, signal } from '@angular/core';

import { ApiService } from './api.service';

export interface OpenInApp {
  /** The id the opener knows, e.g. 'photoshop'. Never derived from user input. */
  id: string;
  label: string;
  /** An existing `IconComponent` name. */
  icon: string;
  /** Lowercase, no dot. */
  extensions: string[];
}

/** Photoshop only for now (#125) — more apps are just another entry here, see #97. */
export const OPEN_IN_APPS: OpenInApp[] = [
  {
    id: 'photoshop',
    label: 'Photoshop',
    icon: 'image',
    extensions: ['jpg', 'jpeg', 'rw2', 'dng', 'insp', 'png', 'tif', 'tiff', 'psd'],
  },
];

export const OPEN_IN_SCHEME = 'footage-archive';

const STORAGE_KEY = 'fa-open-in-enabled';

/**
 * Path relative to `rootDir`, '/'-separated, each segment
 * `encodeURIComponent`'d — null when `path` is not under `rootDir`. Both
 * are normalized to forward slashes first and a trailing slash is stripped
 * from `rootDir`; `path` must start with `rootDir + '/'` on a segment
 * boundary (`/footage/japan` is not under `/footage/jap`), and the
 * remainder must contain at least one non-empty segment (`rootDir` itself
 * maps to null, not to the empty path).
 */
export function encodeRelativePath(path: string, rootDir: string): string | null {
  const normalize = (value: string) => value.replace(/\\/g, '/');
  let root = normalize(rootDir);
  if (root.endsWith('/')) {
    root = root.slice(0, -1);
  }
  const normPath = normalize(path);
  const prefix = `${root}/`;
  if (!normPath.startsWith(prefix)) {
    return null;
  }
  const remainder = normPath.slice(prefix.length);
  const segments = remainder.split('/').filter(segment => segment.length > 0);
  if (segments.length === 0) {
    return null;
  }
  return segments.map(segment => encodeURIComponent(segment)).join('/');
}

/**
 * Hands "open this file in a desktop app" off to a tiny per-device helper
 * ("the Opener") via the custom `footage-archive://` URL scheme, since a
 * browser can't start a desktop app on its own. Each computer needs the
 * Opener installed once (Settings → "Open in"), which maps the scheme to
 * itself and knows the local path to the footage share; this service only
 * builds the URL and navigates to it, fire-and-forget — the Opener reports
 * success or failure itself, via its own native dialogs, not back to this
 * page. See #97 for the concept and #125 for this piece of it.
 */
@Injectable({ providedIn: 'root' })
export class OpenInService {
  private api = inject(ApiService);

  readonly apps = OPEN_IN_APPS;

  readonly enabled = signal<boolean>(this.readStored());

  private rootDir = signal<string | null>(null);

  constructor() {
    this.api.getConfig().subscribe(config => this.rootDir.set(config.root_dir));
  }

  setEnabled(value: boolean): void {
    this.enabled.set(value);
    try {
      localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
    } catch {
      /* storage unavailable (private mode, quota, ...) — still applies this session */
    }
  }

  /** The API returns `file_extension` as `e.suffix.lower()`, i.e. `.jpg`
      with the dot, lowercase (`api/files.py:108`) — still normalized here
      (lowercase, leading dot stripped) so a `FileInfo.file_extension` from
      other endpoints matches the same way. */
  appsFor(fileExtension: string | null | undefined): OpenInApp[] {
    if (!this.enabled()) {
      return [];
    }
    const ext = (fileExtension ?? '').toLowerCase().replace(/^\./, '');
    if (!ext) {
      return [];
    }
    return this.apps.filter(app => app.extensions.includes(ext));
  }

  /** Builds `footage-archive://open?app=<id>&path=<rel>` and navigates to
      it. Returns false (and does nothing) when `rootDir` hasn't loaded yet
      or `path` isn't under it — true otherwise. Fire-and-forget: there is
      no feedback, the Opener itself reports success/failure. */
  open(app: OpenInApp, path: string): boolean {
    const rootDir = this.rootDir();
    if (!rootDir) {
      return false;
    }
    const rel = encodeRelativePath(path, rootDir);
    if (rel === null) {
      return false;
    }
    this.navigate(`${OPEN_IN_SCHEME}://open?app=${encodeURIComponent(app.id)}&path=${rel}`);
    return true;
  }

  test(): void {
    this.navigate(`${OPEN_IN_SCHEME}://test`);
  }

  /** Custom-scheme navigation hands off to the OS and leaves the page
      where it is — it's not a real page load. */
  protected navigate(url: string): void {
    window.location.assign(url);
  }

  /** Used by Settings to fill the real origin into the setup commands. */
  get appOrigin(): string {
    return window.location.origin;
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
