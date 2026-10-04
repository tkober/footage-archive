import { Directive, ElementRef, OnDestroy, afterNextRender, effect, inject, input, output } from '@angular/core';

/**
 * Infinite-scroll sentinel (#40). Put `appInfiniteScroll` on the element
 * that marks "near the bottom of the list" — typically the load-more
 * footer itself — and it fires `reached` whenever that element comes
 * within 300px of the bottom of the nearest scrolling ancestor (found by
 * walking up `offsetParent`-style via `parentElement` until one has
 * `overflow-y: auto|scroll`; falls back to the viewport if none is found,
 * e.g. in a test harness).
 *
 * `disabled` should be bound to "loading, or nothing more to load, or a
 * load-more error pending retry" — while true, `reached` never fires.
 * Binding it to a signal also re-arms the observer when it flips back to
 * false: an `IntersectionObserver` does not refire on its own while its
 * target stays intersecting (e.g. a short page, or a tall viewport that a
 * single loaded page doesn't fill), so each `disabled` → false transition
 * unobserves + re-observes the sentinel, which re-checks its current
 * intersection state and fires again immediately if it's still visible —
 * exactly the "keep loading until the viewport fills or everything is
 * loaded" behaviour the ticket asks for.
 */
@Directive({
  selector: '[appInfiniteScroll]',
  standalone: true,
})
export class InfiniteScrollDirective implements OnDestroy {
  private el = inject(ElementRef<HTMLElement>);

  disabled = input(false);
  reached = output<void>();

  private observer?: IntersectionObserver;

  constructor() {
    afterNextRender(() => this.setup());

    effect(() => {
      if (!this.disabled()) this.reobserve();
    });
  }

  private setup() {
    const root = this.findScrollRoot(this.el.nativeElement);
    this.observer = new IntersectionObserver(
      entries => {
        if (entries[0]?.isIntersecting && !this.disabled()) this.reached.emit();
      },
      { root, rootMargin: '0px 0px 300px 0px' }
    );
    this.observer.observe(this.el.nativeElement);
  }

  /** Unobserve + observe forces the browser to re-evaluate intersection
      immediately, which is what lets a still-visible sentinel keep firing
      after `disabled` clears (see class doc). */
  private reobserve() {
    if (!this.observer) return;
    this.observer.unobserve(this.el.nativeElement);
    this.observer.observe(this.el.nativeElement);
  }

  private findScrollRoot(node: HTMLElement): HTMLElement | null {
    let el = node.parentElement;
    while (el) {
      const overflowY = getComputedStyle(el).overflowY;
      if (overflowY === 'auto' || overflowY === 'scroll') return el;
      el = el.parentElement;
    }
    return null; // no scrolling ancestor found — observe against the viewport
  }

  ngOnDestroy() {
    this.observer?.disconnect();
  }
}
