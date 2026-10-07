import { Component, ElementRef, computed, effect, inject, input, output, signal, viewChild } from '@angular/core';

import { ApiService } from '../../services/api.service';
import { IconComponent } from '../../shared/icon/icon.component';
import { MapMember, MapPoint } from '../../models';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

interface ParsedExifDate {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
}

/** Parses the backend's EXIF-text timestamp ("YYYY:MM:DD HH:MM:SS"). Returns
    null for anything that doesn't match (defensive — the field is typed as
    a plain string). */
function parseExifDate(s: string | null): ParsedExifDate | null {
  if (!s) return null;
  const m = s.match(/^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/);
  if (!m) return null;
  return {
    year: Number(m[1]),
    month: Number(m[2]),
    day: Number(m[3]),
    hour: Number(m[4]),
    minute: Number(m[5]),
  };
}

function formatShortDate(d: ParsedExifDate): string {
  return `${d.day} ${MONTHS[d.month - 1]} ${d.year}`;
}

/** One thumbnail tile in the body grid, plus the "+N" overflow tile. */
interface GridTile {
  member?: MapMember;
  overflowCount?: number;
}

/**
 * App-owned preview panel replacing Google's `MapInfoWindow` (#106). Pure
 * presentation — every action is an output; `MapComponent` owns the map and
 * decides what each one means (open search, fit bounds, open the shared
 * file detail panel).
 */
@Component({
  selector: 'app-map-preview-panel',
  standalone: true,
  imports: [IconComponent],
  templateUrl: './map-preview-panel.component.html',
  styleUrl: './map-preview-panel.component.css',
})
export class MapPreviewPanelComponent {
  private api = inject(ApiService);

  point = input.required<MapPoint>();

  closed = output<void>();
  openSearch = output<void>();
  zoomTo = output<void>();
  openFile = output<MapMember>();

  readonly closeButton = viewChild<ElementRef<HTMLButtonElement>>('closeButton');

  /** Set of member hashes whose `<img>` 404'd — drives the raised
      fallback tile (grid) / stage (single) with the media-type glyph. */
  private readonly failedTilesSignal = signal<Set<string>>(new Set());
  readonly failedTiles = this.failedTilesSignal.asReadonly();
  private readonly singleImgFailedSignal = signal(false);
  readonly singleImgFailed = this.singleImgFailedSignal.asReadonly();

  /** The panel survives marker-to-marker clicks (it's never destroyed), so
      any per-image "failed to load" state from a previous point must be
      dropped when the point itself changes — otherwise a later point
      reusing the same hash (or just a stale Set) could show a fallback
      tile for an image that never actually failed. */
  private readonly resetFailuresOnPointChange = effect(() => {
    this.point();
    this.failedTilesSignal.set(new Set());
    this.singleImgFailedSignal.set(false);
  });

  /** Nice-to-have (#106): move focus to the close button whenever the panel
      (re)opens — including when its content is just replaced by clicking
      another marker, which doesn't destroy/recreate this component, so
      `MapComponent` calls this explicitly after it sets a new point rather
      than relying on an init-only hook. */
  focusClose(): void {
    this.closeButton()?.nativeElement.focus();
  }

  readonly isSingle = computed(() => this.point().count === 1);

  readonly title = computed(() => this.pluralize(this.point().count, 'file'));

  /** "188 photos, 26 videos" — omits a zero part, singular below 2. */
  readonly subLine = computed(() => {
    const p = this.point();
    const parts: string[] = [];
    if (p.photo_count > 0) parts.push(this.pluralize(p.photo_count, 'photo'));
    if (p.video_count > 0) parts.push(this.pluralize(p.video_count, 'video'));
    return parts.join(', ');
  });

  readonly placeChip = computed(() => this.point().place);

  readonly dateChip = computed(() => this.formatDateRange(this.point().date_from, this.point().date_to));

  /** Single-file body date line: "14 Oct 2024, 16:35". */
  readonly singleDateLine = computed(() => {
    const d = parseExifDate(this.point().date_from);
    if (!d) return null;
    const mm = String(d.minute).padStart(2, '0');
    return `${formatShortDate(d)}, ${d.hour}:${mm}`;
  });

  readonly singlePreviewUrl = computed(() => {
    const p = this.point();
    if (p.md5_hash && !this.singleImgFailed()) return this.api.clipPreviewUrl(p.md5_hash);
    return null;
  });

  readonly singleFileName = computed(() => this.point().file_name);

  onSingleImgError(): void {
    this.singleImgFailedSignal.set(true);
  }

  onTileImgError(m: MapMember): void {
    const next = new Set(this.failedTilesSignal());
    next.add(m.md5_hash);
    this.failedTilesSignal.set(next);
  }

  fallbackIcon(m: MapMember): string {
    return m.media_type === 'video' || m.media_type === '360_video' ? 'film' : 'image';
  }

  readonly singleFallbackIcon = computed(() => {
    const mt = this.point().media_type;
    return mt === 'video' || mt === '360_video' ? 'film' : 'image';
  });

  readonly tiles = computed<GridTile[]>(() => {
    const p = this.point();
    const members = p.members ?? [];
    const shown = members.slice(0, 7);
    const tiles: GridTile[] = shown.map(member => ({ member }));
    const overflow = p.count - shown.length;
    if (overflow > 0) tiles.push({ overflowCount: overflow });
    return tiles;
  });

  /** Degenerate bbox (every member effectively at the same spot) — hides
      the "Zoom in" button in favour of a muted hint. Always true for a
      single file (bbox collapses to a point by construction). */
  readonly bboxDegenerate = computed(() => {
    const p = this.point();
    if (p.bbox_west == null || p.bbox_east == null || p.bbox_south == null || p.bbox_north == null) return true;
    return Math.abs(p.bbox_east - p.bbox_west) < 1e-6 && Math.abs(p.bbox_north - p.bbox_south) < 1e-6;
  });

  readonly primaryLabel = computed(() =>
    this.isSingle() ? 'Open details' : `Open in search · ${this.point().count.toLocaleString()}`);

  memberPreview(m: MapMember): string {
    return this.api.clipPreviewUrl(m.md5_hash);
  }

  onPrimary(): void {
    if (this.isSingle()) {
      const p = this.point();
      if (p.md5_hash) this.openFile.emit({ md5_hash: p.md5_hash, file_name: p.file_name!, directory: p.directory!, media_type: p.media_type });
      return;
    }
    this.openSearch.emit();
  }

  onThumbClick(tile: GridTile): void {
    if (tile.member) this.openFile.emit(tile.member);
    else this.openSearch.emit();
  }

  private pluralize(n: number, word: string): string {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  private formatDateRange(from: string | null, to: string | null): string | null {
    if (!from && !to) return null;
    if (from === to) {
      const d = parseExifDate(from);
      return d ? formatShortDate(d) : null;
    }
    if (!from || !to) {
      const d = parseExifDate(from ?? to);
      return d ? formatShortDate(d) : null;
    }
    const f = parseExifDate(from);
    const t = parseExifDate(to);
    if (!f || !t) return null;
    if (f.year === t.year && f.month === t.month) {
      return `${f.day}–${t.day} ${MONTHS[f.month - 1]} ${f.year}`;
    }
    if (f.year === t.year) {
      return `${f.day} ${MONTHS[f.month - 1]} – ${t.day} ${MONTHS[t.month - 1]} ${f.year}`;
    }
    return `${formatShortDate(f)} – ${formatShortDate(t)}`;
  }
}
