import { Injectable, signal } from '@angular/core';

/** One breadcrumb segment in the topbar. `action` navigates there when
    clicked; the last item in the list has no action and renders bold
    (the current location). A plain title (no navigation) is just a
    single-item array with no `action`. */
export interface HeaderCrumb {
  label: string;
  action?: () => void;
}

/**
 * Lets the active page drive the topbar's left-hand header slot instead of
 * rendering its own H2/breadcrumbs. Call `setCrumbs()` from a page's
 * `ngOnInit` (or an effect reacting to its own state, e.g. the browser's
 * current path); the app shell falls back to the current route's `title`
 * once the page clears its crumbs (on destroy) or never sets any.
 */
@Injectable({ providedIn: 'root' })
export class HeaderService {
  private readonly _crumbs = signal<HeaderCrumb[] | null>(null);

  /** `null` while no page has published anything — the shell shows the route title instead. */
  readonly crumbs = this._crumbs.asReadonly();

  setCrumbs(crumbs: HeaderCrumb[]): void {
    this._crumbs.set(crumbs);
  }

  /** Convenience for a page with no breadcrumb trail, just a title. */
  setTitle(title: string): void {
    this._crumbs.set([{ label: title }]);
  }

  /** Back to the route-title fallback. Called by the shell on every
      navigation, before the next page's component runs its own ngOnInit. */
  clear(): void {
    this._crumbs.set(null);
  }
}
