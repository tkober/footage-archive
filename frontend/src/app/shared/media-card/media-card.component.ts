import { Component, computed, effect, input, output, signal } from '@angular/core';
import { IconComponent } from '../icon/icon.component';

export type MediaCardKind = 'video' | 'photo' | 'other';

/**
 * Shared grid tile (#39) for a single file — browser, search results and
 * list-detail all render this instead of their own copy. Video keeps the
 * full 5-frame filmstrip (never a single-frame crop); photo is a 3:2 cover
 * crop; `other` covers non-media untracked files (no preview at all, e.g.
 * `.DS_Store`) and renders a plain file-icon tile.
 *
 * Rename is handled by the caller: when `renaming` is true the card renders
 * the projected content (the caller's inline rename `<input>` + save/cancel
 * buttons) instead of the name/meta caption, keeping existing rename
 * behaviour working unchanged.
 */
@Component({
  selector: 'app-media-card',
  standalone: true,
  imports: [IconComponent],
  templateUrl: './media-card.component.html',
  styleUrl: './media-card.component.css',
})
export class MediaCardComponent {
  kind = input<MediaCardKind>('photo');
  name = input('');
  extension = input<string | null>(null);
  previewUrl = input<string | null>(null);
  tracked = input<boolean | null>(true);
  /** Already formatted, e.g. "00:12" or "1:02:03" — see `formatDuration()` callers. */
  duration = input<string | null>(null);
  /** Show the extension badge on the image (photo only) — caller decides
      based on whether the folder mixes formats. */
  showExt = input(false);
  /** List/search: mono accent item code shown before the name. */
  code = input<string | null>(null);
  /** Search: recorded/added date shown after the name. */
  date = input<string | null>(null);
  /** Detail-open highlight (single-select, browser/search/list-detail). */
  selected = input(false);
  /** Checked in bulk-selection mode. */
  bulkSelected = input(false);
  /** Bulk-selection mode is on (keeps the check circle visible). */
  selecting = input(false);
  /** Caller is rendering an inline rename input in the projected slot. */
  renaming = input(false);
  /** Renders a shimmering placeholder tile instead (#40) — used for the
      "next page" preview while infinite-scroll is loading. `kind` still
      controls the thumb's aspect ratio (video keeps the filmstrip shape);
      every other input is ignored. */
  skeleton = input(false);

  /** Click / Enter on the tile; the event carries Shift/Cmd/Ctrl for range
      and multi selection (#42). */
  open = output<MouseEvent | KeyboardEvent>();
  toggleSelect = output<MouseEvent>();
  contextMenu = output<MouseEvent>();
  /** The "⋯" button element, so the caller can anchor its context menu. */
  more = output<HTMLElement>();

  /** Preview failed to load (404, decode error, …) — falls back to the
      "Generating preview…" skeleton tile, never a broken-image icon. */
  imgError = signal(false);

  displayName = computed(() => {
    const n = this.name();
    return this.kind() === 'photo' || this.kind() === 'video'
      ? n.replace(/\.[^./]+$/, '')
      : n;
  });

  showPending = computed(() => this.kind() !== 'other' && (!this.previewUrl() || this.imgError()));

  /** ".jpg" → "JPG" for badges and the video caption. */
  extLabel = computed(() => (this.extension() ?? '').replace(/^\./, '').toUpperCase());

  meta = computed(() => {
    if (this.kind() !== 'video') return null;
    const ext = this.extLabel();
    const dur = this.duration();
    return [ext, dur].filter(Boolean).join(' · ') || null;
  });

  /** A new preview URL (incl. going back to null during a reload) means any
      earlier load error no longer applies. */
  private resetErrorOnUrlChange = effect(() => {
    this.previewUrl();
    this.imgError.set(false);
  });

  onImgError() {
    this.imgError.set(true);
  }

  onClick(event: MouseEvent | KeyboardEvent) {
    this.open.emit(event);
  }

  onToggleSelect(event: MouseEvent) {
    event.stopPropagation();
    this.toggleSelect.emit(event);
  }

  onMore(event: MouseEvent) {
    event.stopPropagation();
    this.more.emit(event.currentTarget as HTMLElement);
  }

  onContextMenu(event: MouseEvent) {
    this.contextMenu.emit(event);
  }
}
