import {
  Component, ElementRef, HostListener,
  computed, effect, input, output, signal, viewChild
} from '@angular/core';

/**
 * Reusable single-image viewer with zoom (wheel/buttons/keyboard), drag-to-pan,
 * and optional prev/next navigation (on-screen arrows + arrow keys). Mirrors the
 * step-through experience of the comparison view so the two share a look & feel.
 * The host fills its container; the parent decides the bounded height.
 */
@Component({
  selector: 'app-image-viewer',
  standalone: true,
  templateUrl: './image-viewer.component.html',
  styleUrl: './image-viewer.component.css',
})
export class ImageViewerComponent {
  /** Image URL to display (preview or full-resolution). */
  src = input<string | null>(null);

  /** Show the on-screen ‹ › arrows + counter and enable arrow-key navigation. */
  showNav = input<boolean>(false);
  index   = input<number>(0);   // 0-based position in the navigable set
  count   = input<number>(0);   // total navigable items

  prev = output<void>();
  next = output<void>();

  private stage = viewChild<ElementRef<HTMLElement>>('stage');

  // Zoom / pan
  zoom = signal(1);
  panX = signal(0);
  panY = signal(0);

  imgTransform = computed(() => `translate(${this.panX()}px, ${this.panY()}px) scale(${this.zoom()})`);
  zoomPct      = computed(() => Math.round(this.zoom() * 100));
  hasPrev      = computed(() => this.index() > 0);
  hasNext      = computed(() => this.index() < this.count() - 1);

  constructor() {
    // A freshly loaded image (sibling navigation or HQ swap) starts at 100%.
    effect(() => { this.src(); this.resetZoom(); });
  }

  // --- Navigation ----------------------------------------------------------
  emitPrev() { if (this.hasPrev()) this.prev.emit(); }
  emitNext() { if (this.hasNext()) this.next.emit(); }

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

  // --- Pointer pan (only when zoomed) --------------------------------------
  private panning = false;
  private panRect?: DOMRect;
  private panStart = { x: 0, y: 0, px: 0, py: 0 };

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
    if (this.panning) this.updatePan(e.clientX, e.clientY);
  }

  @HostListener('document:pointerup')
  onPointerUp() { this.panning = false; }

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
    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowLeft':  if (this.showNav()) { this.emitPrev(); e.preventDefault(); } break;
      case 'ArrowDown':
      case 'ArrowRight': if (this.showNav()) { this.emitNext(); e.preventDefault(); } break;
      case '+': case '=': this.zoomIn(); e.preventDefault(); break;
      case '-': case '_': this.zoomOut(); e.preventDefault(); break;
      case '0': this.resetZoom(); break;
    }
  }
}
