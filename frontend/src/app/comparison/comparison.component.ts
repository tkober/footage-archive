import {
  Component, ElementRef, HostListener, OnInit, OnDestroy,
  computed, inject, input, output, signal, viewChild
} from '@angular/core';
import { from, of, Subscription, catchError, map, mergeMap } from 'rxjs';

import { ApiService } from '../services/api.service';
import { ToastService } from '../shared/toast/toast.service';
import { MenuComponent, MenuItem, MenuPoint } from '../shared/menu/menu.component';
import { PopoverComponent } from '../shared/popover/popover.component';
import { KeywordPickerComponent } from '../shared/keyword-picker/keyword-picker.component';
import { ListPickerComponent } from '../shared/list-picker/list-picker.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { IconComponent } from '../shared/icon/icon.component';
import { DeletePreviewResponse, FileList, PathChild, formatDeletePreview } from '../models';

type Mode = 'step' | 'side' | 'overlay';

/** Pending delete-to-trash awaiting confirmation (#146), for one filmstrip entry. */
interface PendingDelete {
  idx: number;
  path: string;
  preview: DeletePreviewResponse;
}

/**
 * Full-screen overlay for comparing a set of similar photos to pick the best shot.
 * Opened from the browser's multiselect. The filmstrip uses the ~600px clip previews;
 * the main stage can fetch full-resolution versions on demand and zoom/pan into them.
 * Three modes: step-through, side-by-side slider, and a blended overlay.
 */
@Component({
  selector: 'app-comparison-view',
  standalone: true,
  imports: [MenuComponent, PopoverComponent, KeywordPickerComponent, ListPickerComponent, ConfirmDialogComponent, IconComponent],
  templateUrl: './comparison.component.html',
  styleUrl: './comparison.component.css'
})
export class ComparisonComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private toast = inject(ToastService);
  private el = inject(ElementRef<HTMLElement>);

  /** Selected photos to compare (snapshotted by the parent at open time). */
  items  = input.required<PathChild[]>();
  closed = output<void>();
  /** Move-to-trash (#146): paths actually deleted, so the browser can
      reconcile its grid + bulk selection (same shape as the detail panel's
      `deleted`, just batched since several photos can be trashed one at a
      time without the comparison ever closing in between). */
  deleted = output<string[]>();

  private stage = viewChild<ElementRef<HTMLElement>>('stage');

  // Working list (own copy so in-modal removals don't touch the browser selection)
  entries   = signal<PathChild[]>([]);
  mode      = signal<Mode>('step');
  focusIdx  = signal(0);   // active list item: drives step display + a/b/x/keyword/list/trash targeting
  aIdx      = signal(0);
  bIdx      = signal(1);
  sliderPct = signal(50);  // Mode B divider, 0–100
  blendPct  = signal(50);  // Mode C cross-fade (A opacity), 0–100
  diffMode  = signal(false);

  // Context menu (#146): opened by right-click (at the pointer) or a filmstrip
  // item's "⋯" button (anchored to it) — same `app-menu` the rest of the app
  // uses. `menuSource` is whichever triggered it, used to restore focus and
  // to anchor the keyword/list popover when it's opened from the menu.
  menuIdx    = signal<number | null>(null);
  menuPoint  = signal<MenuPoint | null>(null);
  menuAnchor = signal<HTMLElement | null>(null);
  private menuSource: HTMLElement | null = null;
  menuItems  = computed<MenuItem[]>(() => this.menuIdx() !== null ? this.menuItemsFor() : []);
  menuHeaderEntry = computed(() => {
    const idx = this.menuIdx();
    return idx !== null ? this.entries()[idx] ?? null : null;
  });

  // "Add keyword…" / "Add to list…" popover (#146), anchored at the header
  // button or at the filmstrip item it was triggered from.
  popover = signal<{ kind: 'keyword' | 'list'; idx: number; anchor: HTMLElement } | null>(null);

  // Move to trash (#146): preview → confirm → delete, same flow as the
  // browser/file-detail-panel, scoped to one filmstrip entry at a time.
  pendingDelete = signal<PendingDelete | null>(null);
  rootDir      = signal('');
  trashDirName = signal('.trash');

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

  /** Filmstrip thumbnails stay low-res; also used for the context menu's header thumb. */
  thumbUrl(it?: PathChild): string | null {
    return it?.md5_hash ? this.api.clipPreviewUrl(it.md5_hash) : null;
  }

  /** Whether this photo's full-resolution version has been fetched & cached. */
  hasHq(it: PathChild): boolean {
    return !!it.md5_hash && this.hqUrls().has(it.md5_hash);
  }

  ngOnInit() {
    this.entries.set([...this.items()]);
    this.api.getConfig().subscribe(cfg => { this.rootDir.set(cfg.root_dir); this.trashDirName.set(cfg.trash_dir_name); });
    // Teleport to <body> so the fixed backdrop escapes the sliding .detail-view transform.
    document.body.appendChild(this.el.nativeElement);
    // #146: this overlay sits above the menu/popover/modal/toast layers, so
    // anything opened *from* it (its own context menu, keyword/list popover,
    // trash confirm dialog, toasts) needs those layers bumped above it in
    // turn — see `body.cmp-open` in src/styles.css.
    document.body.classList.add('cmp-open');
  }

  ngOnDestroy() {
    document.body.classList.remove('cmp-open');
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

  // --- Context menu (#146) --------------------------------------------------

  private openMenu(i: number, opts: { point?: MenuPoint; anchor?: HTMLElement }) {
    this.popover.set(null);
    this.menuSource = opts.anchor ?? this.stripItemEl(i);
    this.menuPoint.set(opts.point ?? null);
    this.menuAnchor.set(opts.anchor ?? null);
    this.menuIdx.set(i);
  }

  openCtx(event: MouseEvent, i: number) {
    event.preventDefault();
    this.openMenu(i, { point: { x: event.clientX, y: event.clientY } });
  }

  onStripMore(event: MouseEvent, i: number) {
    event.stopPropagation();
    this.openMenu(i, { anchor: event.currentTarget as HTMLElement });
  }

  /** Closed without picking (Esc, click outside): give focus back to whatever opened it. */
  closeMenu() {
    const had = this.menuIdx() !== null;
    this.menuIdx.set(null);
    this.menuAnchor.set(null);
    this.menuPoint.set(null);
    if (had) this.focusSource(this.menuSource);
  }

  onMenuSelect(id: string) {
    const idx = this.menuIdx();
    const source = this.menuSource;
    this.menuIdx.set(null);
    this.menuAnchor.set(null);
    this.menuPoint.set(null);
    if (idx === null) return;
    switch (id) {
      case 'a':       this.assignA(idx); break;
      case 'b':       this.assignB(idx); break;
      case 'keyword': this.openKeywordPopover(idx, source ?? this.stripItemEl(idx)); break;
      case 'list':    this.openListPopover(idx, source ?? this.stripItemEl(idx)); break;
      case 'remove':  this.removeAt(idx); break;
      case 'delete':  this.requestDelete(idx); break;
    }
  }

  /** Same ids drive the single-key shortcuts in `onKey` below. "Set as A/B"
      stay available even outside side/overlay mode (today's behaviour). */
  private menuItemsFor(): MenuItem[] {
    return [
      { id: 'a', label: 'Set as A', shortcut: 'A' },
      { id: 'b', label: 'Set as B', shortcut: 'B' },
      { id: 'keyword', label: 'Add keyword…', icon: 'tag', shortcut: 'K', separatorBefore: true },
      { id: 'list', label: 'Add to list…', icon: 'list', shortcut: 'L' },
      { id: 'remove', label: 'Remove from comparison', icon: 'x', shortcut: 'X', separatorBefore: true },
      { id: 'delete', label: 'Move to trash', icon: 'trash', danger: true, shortcut: 'Delete' },
    ];
  }

  /** "Still · JPG" — same shape as the browser's `entryTypeLabel` for a
      tracked photo (comparison entries are always tracked stills). */
  entryTypeLabel(entry: PathChild): string {
    const ext = (entry.file_extension ?? '').replace(/^\./, '').toUpperCase();
    return ['Still', ext].filter(Boolean).join(' · ');
  }

  private stripItemEl(idx: number): HTMLElement | null {
    return this.el.nativeElement.querySelector(`[data-idx="${idx}"]`) as HTMLElement | null;
  }

  private focusSource(el: HTMLElement | null) {
    if (!el || !el.isConnected) return;
    (el.matches('[tabindex], button') ? el : el.querySelector<HTMLElement>('[tabindex], button'))?.focus();
  }

  // --- "Add keyword…" / "Add to list…" popover (#146) -----------------------

  openKeywordPopover(idx: number, anchor: HTMLElement | null) {
    if (!anchor) return;
    this.popover.set({ kind: 'keyword', idx, anchor });
  }

  openListPopover(idx: number, anchor: HTMLElement | null) {
    if (!anchor) return;
    this.popover.set({ kind: 'list', idx, anchor });
  }

  closePopover() {
    const p = this.popover();
    this.popover.set(null);
    if (p) this.focusSource(p.anchor);
  }

  submitPopoverKeyword(keyword: string) {
    const p = this.popover();
    const kw = keyword.trim();
    const entry = p ? this.entries()[p.idx] : null;
    if (p && kw && entry?.md5_hash) {
      this.api.addKeyword(entry.md5_hash, kw).subscribe({
        next: () => this.toast.show(`Added “${kw}” to ${entry.name}`),
        error: () => this.toast.show(`Couldn't add “${kw}” to ${entry.name}`),
      });
    }
    this.closePopover();
  }

  popoverAddToList(list: FileList) {
    const p = this.popover();
    const entry = p ? this.entries()[p.idx] : null;
    if (p && entry?.md5_hash) {
      this.api.addFilesToList(list.id, [entry.md5_hash]).subscribe({
        next: resp => this.toast.show(resp.added.length
          ? `Added ${entry.name} to ‘${list.name}’`
          : `${entry.name} is already in ‘${list.name}’`),
        error: () => this.toast.show(`Couldn't add ${entry.name} to ‘${list.name}’`),
      });
    }
    this.closePopover();
  }

  // --- Move to trash (#146) -------------------------------------------------

  /** Always previews first; a failed preview just toasts the error (same
      convention as the browser/file-detail-panel — no confirm without it). */
  requestDelete(idx: number) {
    const entry = this.entries()[idx];
    if (!entry) return;
    this.api.previewDelete([entry.path]).subscribe({
      next: preview => this.pendingDelete.set({ idx, path: entry.path, preview }),
      error: err => this.toast.show(err.error?.detail ?? 'Could not preview the delete'),
    });
  }

  deletePreviewMessage() {
    const p = this.pendingDelete();
    if (!p) return { message: '', warning: null as string | null };
    return formatDeletePreview(p.preview, this.rootDir(), this.trashDirName());
  }

  cancelPendingDelete() {
    this.pendingDelete.set(null);
  }

  confirmPendingDelete() {
    const p = this.pendingDelete();
    if (!p) return;
    this.pendingDelete.set(null);
    this.api.deleteFiles([p.path]).subscribe({
      next: resp => {
        const result = resp.results[0];
        if (result?.ok) {
          this.toast.show('1 item moved to trash');
          this.deleted.emit([p.path]);
          // Look the entry back up by path rather than trusting `idx` — it's
          // been sitting in a confirm dialog, during which nothing else in
          // the comparison can change (its menu/popover are blocked behind
          // the dialog), so this is just defensive.
          const curIdx = this.entries().findIndex(e => e.path === p.path);
          if (curIdx >= 0) this.removeAt(curIdx);
        } else {
          this.toast.show(result?.error ?? 'Failed to move to trash');
        }
      },
      error: err => this.toast.show(err.error?.detail ?? 'Failed to move to trash'),
    });
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
    // Esc must close just the innermost layer, never the whole comparison on
    // the same keypress (#146). `app-menu`/`app-popover` (and, bubbling up
    // through it, the inline keyword/list picker's own Esc handling) mark
    // the event handled before it reaches us; the trash confirm dialog's
    // underlying ModalComponent has its own document-level Esc listener that
    // cancels it but never marks the event, so every layer is also checked
    // directly as a belt-and-braces guard.
    if (e.key === 'Escape') {
      if (e.defaultPrevented || this.menuIdx() !== null || this.popover() || this.pendingDelete()) return;
      this.closed.emit();
      e.stopPropagation();
      return;
    }
    if (this.menuIdx() !== null || this.popover() || this.pendingDelete()) return;

    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowLeft':  this.moveFocus(-1); e.preventDefault(); break;
      case 'ArrowDown':
      case 'ArrowRight': this.moveFocus(+1); e.preventDefault(); break;
      case 'a': case 'A': this.assignA(this.focusIdx()); break;
      case 'b': case 'B': this.assignB(this.focusIdx()); break;
      case 'k': case 'K': this.openKeywordPopover(this.focusIdx(), this.stripItemEl(this.focusIdx())); break;
      case 'l': case 'L': this.openListPopover(this.focusIdx(), this.stripItemEl(this.focusIdx())); break;
      case 'x': case 'X': this.removeAt(this.focusIdx()); break;
      case 'Delete':
      case 'Backspace': this.requestDelete(this.focusIdx()); e.preventDefault(); break;
      case '+': case '=': this.zoomIn(); e.preventDefault(); break;
      case '-': case '_': this.zoomOut(); e.preventDefault(); break;
      case '0': this.resetZoom(); break;
    }
  }
}
