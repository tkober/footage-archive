import {
  Component, ElementRef, HostListener, OnInit, OnDestroy,
  computed, inject, input, output, signal, viewChild
} from '@angular/core';

import { ApiService } from '../services/api.service';
import { PathChild } from '../models';

type Mode = 'step' | 'side' | 'overlay';

/**
 * Full-screen overlay for comparing a set of similar photos to pick the best shot.
 * Opened from the browser's multiselect; sourced from the existing ~600px clip previews.
 * Three modes: step-through, side-by-side slider, and a blended overlay.
 */
@Component({
  selector: 'app-comparison-view',
  standalone: true,
  templateUrl: './comparison.component.html',
  styleUrl: './comparison.component.css'
})
export class ComparisonComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private el = inject(ElementRef<HTMLElement>);

  /** Selected photos to compare (snapshotted by the parent at open time). */
  items  = input.required<PathChild[]>();
  closed = output<void>();

  private stage = viewChild<ElementRef<HTMLElement>>('stage');

  // Working list (own copy so in-modal removals don't touch the browser selection)
  entries   = signal<PathChild[]>([]);
  mode      = signal<Mode>('step');
  focusIdx  = signal(0);   // active list item: drives step display + a/b/x targeting
  aIdx      = signal(0);
  bIdx      = signal(1);
  sliderPct = signal(50);  // Mode B divider, 0–100
  blendPct  = signal(50);  // Mode C cross-fade (A opacity), 0–100
  diffMode  = signal(false);
  ctxMenu   = signal<{ idx: number; x: number; y: number } | null>(null);

  count    = computed(() => this.entries().length);
  needsTwo = computed(() => this.mode() !== 'step');
  focusUrl = computed(() => this.urlFor(this.entries()[this.focusIdx()]));
  aUrl     = computed(() => this.urlFor(this.entries()[this.aIdx()]));
  bUrl     = computed(() => this.urlFor(this.entries()[this.bIdx()]));
  clipA    = computed(() => `inset(0 ${100 - this.sliderPct()}% 0 0)`);

  private urlFor(it?: PathChild): string | null {
    return it?.md5_hash ? this.api.clipPreviewUrl(it.md5_hash) : null;
  }

  /** Public helper for the filmstrip thumbnails. */
  thumbUrl(it: PathChild): string | null {
    return this.urlFor(it);
  }

  ngOnInit() {
    this.entries.set([...this.items()]);
    // Teleport to <body> so the fixed backdrop escapes the sliding .detail-view transform.
    document.body.appendChild(this.el.nativeElement);
  }

  ngOnDestroy() {
    this.el.nativeElement.remove();
  }

  setMode(m: Mode) {
    if ((m === 'side' || m === 'overlay') && this.count() < 2) return;
    this.mode.set(m);
  }

  moveFocus(d: number) {
    this.focusIdx.update(i => Math.min(Math.max(i + d, 0), this.count() - 1));
  }

  // Keep A and B distinct — picking the other slot's image swaps them.
  assignA(i: number) { if (i === this.bIdx()) this.bIdx.set(this.aIdx()); this.aIdx.set(i); }
  assignB(i: number) { if (i === this.aIdx()) this.aIdx.set(this.bIdx()); this.bIdx.set(i); }

  removeAt(i: number) {
    this.entries.update(l => l.filter((_, k) => k !== i));
    const last = this.count() - 1;
    if (last < 0) { this.closed.emit(); return; }        // empty → auto-close
    const adj = (idx: number) => (idx > i ? idx - 1 : idx); // keep same images assigned
    this.focusIdx.set(Math.min(adj(this.focusIdx()), last));
    this.aIdx.set(Math.min(adj(this.aIdx()), last));
    this.bIdx.set(Math.min(adj(this.bIdx()), last));
    if (this.aIdx() === this.bIdx() && last >= 1) this.bIdx.set(this.aIdx() === 0 ? 1 : 0);
    if (this.count() < 2) this.mode.set('step');         // side/overlay need two
  }

  openCtx(event: MouseEvent, i: number) {
    event.preventDefault();
    this.ctxMenu.set({ idx: i, x: event.clientX, y: event.clientY });
  }

  // --- Mode B: divider drag ------------------------------------------------
  private dragging = false;
  private rect?: DOMRect;

  startDrag(e: PointerEvent) {
    const el = this.stage()?.nativeElement;
    if (!el) return;
    this.dragging = true;
    this.rect = el.getBoundingClientRect();
    this.updateSlider(e.clientX);
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    e.preventDefault();
  }

  @HostListener('document:pointermove', ['$event'])
  onPointerMove(e: PointerEvent) {
    if (!this.dragging) return;
    this.updateSlider(e.clientX);
  }

  @HostListener('document:pointerup')
  onPointerUp() { this.dragging = false; }

  private updateSlider(clientX: number) {
    if (!this.rect || !this.rect.width) return;
    const pct = ((clientX - this.rect.left) / this.rect.width) * 100;
    this.sliderPct.set(Math.min(100, Math.max(0, pct)));
  }

  // --- Keyboard ------------------------------------------------------------
  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent) {
    if (this.ctxMenu()) {
      if (e.key === 'Escape') this.ctxMenu.set(null);
      return;
    }
    if (e.key === 'Escape') { this.closed.emit(); e.stopPropagation(); return; }

    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowLeft':  this.moveFocus(-1); e.preventDefault(); break;
      case 'ArrowDown':
      case 'ArrowRight': this.moveFocus(+1); e.preventDefault(); break;
      case 'a': case 'A': this.assignA(this.focusIdx()); break;
      case 'b': case 'B': this.assignB(this.focusIdx()); break;
      case 'x': case 'X': this.removeAt(this.focusIdx()); break;
    }
  }
}
