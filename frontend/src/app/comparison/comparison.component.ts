import {
  Component, ElementRef, HostListener, OnInit, OnDestroy,
  computed, inject, input, output, signal, viewChild
} from '@angular/core';
import { from, of, Subscription, catchError, map, mergeMap } from 'rxjs';

import { ApiService } from '../services/api.service';
import { PathChild } from '../models';

type Mode = 'step' | 'side' | 'overlay';

/**
 * Full-screen overlay for comparing a set of similar photos to pick the best shot.
 * Opened from the browser's multiselect. The filmstrip uses the ~600px clip previews;
 * the main stage can fetch full-resolution versions on demand and zoom/pan into them.
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

  // High-quality (full-resolution) sources
  hqMode     = signal(false);
  hqUrls     = signal<Map<string, string>>(new Map());   // md5 → object URL
  hqProgress = signal({ loaded: 0, total: 0 });
  hqFetching = signal(false);

  // Zoom / pan (shared across A & B so they stay aligned)
  zoom = signal(1);
  panX = signal(0);
  panY = signal(0);

  count    = computed(() => this.entries().length);
  needsTwo = computed(() => this.mode() !== 'step');
  focusUrl = computed(() => this.stageUrlFor(this.entries()[this.focusIdx()]));
  aUrl     = computed(() => this.stageUrlFor(this.entries()[this.aIdx()]));
  bUrl     = computed(() => this.stageUrlFor(this.entries()[this.bIdx()]));
  clipA    = computed(() => `inset(0 ${100 - this.sliderPct()}% 0 0)`);
  imgTransform = computed(() => `translate(${this.panX()}px, ${this.panY()}px) scale(${this.zoom()})`);
  zoomPct  = computed(() => Math.round(this.zoom() * 100));

  /** Main-stage source: full-res object URL when HQ is on & cached, else the 600px preview. */
  private stageUrlFor(it?: PathChild): string | null {
    if (!it?.md5_hash) return null;
    if (this.hqMode()) {
      const u = this.hqUrls().get(it.md5_hash);
      if (u) return u;
    }
    return this.api.clipPreviewUrl(it.md5_hash);
  }

  /** Filmstrip thumbnails stay low-res. */
  thumbUrl(it: PathChild): string | null {
    return it?.md5_hash ? this.api.clipPreviewUrl(it.md5_hash) : null;
  }

  /** Whether this photo's full-resolution version has been fetched & cached. */
  hasHq(it: PathChild): boolean {
    return !!it.md5_hash && this.hqUrls().has(it.md5_hash);
  }

  ngOnInit() {
    this.entries.set([...this.items()]);
    // Teleport to <body> so the fixed backdrop escapes the sliding .detail-view transform.
    document.body.appendChild(this.el.nativeElement);
  }

  ngOnDestroy() {
    this.hqSub?.unsubscribe();
    for (const url of this.hqUrls().values()) URL.revokeObjectURL(url);
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

  // --- High-quality preload ------------------------------------------------
  private hqSub?: Subscription;

  fetchHighQuality() {
    if (this.hqFetching()) return;
    if (this.hqMode()) { this.hqMode.set(false); return; }   // toggle back to previews

    const all = this.entries();
    const cached = this.hqUrls();
    const missing = all.filter(it => it.md5_hash && !cached.has(it.md5_hash));
    if (missing.length === 0) { this.hqMode.set(true); return; }

    this.hqFetching.set(true);
    this.hqProgress.set({ loaded: all.length - missing.length, total: all.length });
    this.hqSub = from(missing).pipe(
      mergeMap(it => this.api.fetchFullImage(it.md5_hash!).pipe(
        map(blob => ({ it, blob: blob as Blob | null })),
        catchError(() => of({ it, blob: null as Blob | null })),   // skip failures, keep the batch going
      ), 3),                                                        // ≤3 requests in flight
    ).subscribe({
      next: ({ it, blob }) => {
        if (blob) {
          const url = URL.createObjectURL(blob);
          this.hqUrls.update(m => { const n = new Map(m); n.set(it.md5_hash!, url); return n; });
        }
        this.hqProgress.update(p => ({ ...p, loaded: p.loaded + 1 }));
      },
      complete: () => { this.hqMode.set(true); this.hqFetching.set(false); },
    });
  }

  // --- Zoom / pan ----------------------------------------------------------
  zoomIn()    { this.setZoom(this.zoom() * 1.4); }
  zoomOut()   { this.setZoom(this.zoom() / 1.4); }
  resetZoom() { this.zoom.set(1); this.panX.set(0); this.panY.set(0); }

  private setZoom(z: number) {
    const nz = Math.min(8, Math.max(1, z));
    this.zoom.set(nz);
    if (nz === 1) { this.panX.set(0); this.panY.set(0); } else this.clampPan();
  }

  onWheel(e: WheelEvent) {
    e.preventDefault();
    if (e.deltaY < 0) this.zoomIn(); else this.zoomOut();
  }

  // --- Pointer: divider drag (side mode) + pan (any mode when zoomed) -------
  private dragging = false;
  private panning = false;
  private rect?: DOMRect;
  private panRect?: DOMRect;
  private panStart = { x: 0, y: 0, px: 0, py: 0 };

  startDrag(e: PointerEvent) {
    const el = this.stage()?.nativeElement;
    if (!el) return;
    this.dragging = true;
    this.rect = el.getBoundingClientRect();
    this.updateSlider(e.clientX);
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    e.stopPropagation();   // don't also start a pan
    e.preventDefault();
  }

  startPan(e: PointerEvent) {
    if (this.zoom() <= 1) return;
    this.panning = true;
    this.panRect = this.stage()?.nativeElement.getBoundingClientRect();
    this.panStart = { x: e.clientX, y: e.clientY, px: this.panX(), py: this.panY() };
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    e.preventDefault();
  }

  @HostListener('document:pointermove', ['$event'])
  onPointerMove(e: PointerEvent) {
    if (this.dragging) { this.updateSlider(e.clientX); return; }
    if (this.panning) { this.updatePan(e.clientX, e.clientY); }
  }

  @HostListener('document:pointerup')
  onPointerUp() { this.dragging = false; this.panning = false; }

  private updateSlider(clientX: number) {
    if (!this.rect || !this.rect.width) return;
    const pct = ((clientX - this.rect.left) / this.rect.width) * 100;
    this.sliderPct.set(Math.min(100, Math.max(0, pct)));
  }

  private updatePan(clientX: number, clientY: number) {
    let nx = this.panStart.px + (clientX - this.panStart.x);
    let ny = this.panStart.py + (clientY - this.panStart.y);
    if (this.panRect) {
      const maxX = (this.zoom() - 1) * this.panRect.width / 2;
      const maxY = (this.zoom() - 1) * this.panRect.height / 2;
      nx = Math.min(maxX, Math.max(-maxX, nx));
      ny = Math.min(maxY, Math.max(-maxY, ny));
    }
    this.panX.set(nx);
    this.panY.set(ny);
  }

  private clampPan() {
    const r = this.stage()?.nativeElement.getBoundingClientRect();
    if (!r) return;
    const maxX = (this.zoom() - 1) * r.width / 2;
    const maxY = (this.zoom() - 1) * r.height / 2;
    this.panX.update(x => Math.min(maxX, Math.max(-maxX, x)));
    this.panY.update(y => Math.min(maxY, Math.max(-maxY, y)));
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
      case '+': case '=': this.zoomIn(); e.preventDefault(); break;
      case '-': case '_': this.zoomOut(); e.preventDefault(); break;
      case '0': this.resetZoom(); break;
    }
  }
}
