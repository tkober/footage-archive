import { Component, computed, input, output } from '@angular/core';

/**
 * Infinite-scroll footer (#40), shared by browser/search/list-detail.
 * Doubles as the `appInfiniteScroll` sentinel (applied by the caller
 * directly on `<app-load-more-footer>`): it sits at the bottom of the
 * grid, so coming within the directive's 300px lookahead margin is a
 * reasonable proxy for "near the end of the list" regardless of which
 * state (progress/button, loading, error, or the end-divider) is showing.
 *
 * States: more to load → progress bar + "N of M" + "Load X more" button
 * (the button is a fallback for keyboard users / when auto-load is
 * paused); loading → button reads "Loading…"; error → short message +
 * "Retry" (auto-loading stays paused until the caller clears the error);
 * all loaded → a quiet "All N items · <endLabel>" divider. Hidden
 * entirely when there are 0 items.
 */
@Component({
  selector: 'app-load-more-footer',
  standalone: true,
  templateUrl: './load-more-footer.component.html',
  styleUrl: './load-more-footer.component.css',
})
export class LoadMoreFooterComponent {
  /** Items loaded so far. */
  shown = input(0);
  /** Total items available. */
  total = input(0);
  /** A page fetch (initial load-more, or a retry) is in flight. */
  loading = input(false);
  /** Short message to show instead of the progress/button row; non-null
      pauses auto-loading until the caller clears it (e.g. on retry). */
  error = input<string | null>(null);
  /** Trailing phrase after "All N items · " — varies per page
      ("end of folder" / "end of results" / "end of list"). */
  endLabel = input('end of folder');
  /** The caller's page size — caps the "Load X more" button label
      (the actual next page may return fewer than this if it's the last one). */
  pageSize = input(50);

  loadMore = output<void>();
  retry = output<void>();

  hasMore = computed(() => this.shown() < this.total());
  nextCount = computed(() => Math.max(0, Math.min(this.pageSize(), this.total() - this.shown())));
  pct = computed(() => this.total() > 0 ? Math.round((this.shown() / this.total()) * 100) : 0);
}
