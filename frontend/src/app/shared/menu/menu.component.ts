import {
  AfterViewInit,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  QueryList,
  ViewChild,
  ViewChildren,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

import { IconComponent } from '../icon/icon.component';

export interface MenuItem {
  id: string;
  label: string;
  icon?: string;
  shortcut?: string;
  danger?: boolean;
  disabled?: boolean;
  /** Renders a thin separator line above this item. */
  separatorBefore?: boolean;
}

export interface MenuPoint {
  x: number;
  y: number;
}

/**
 * Floating menu (context menu / dropdown), positioned either at a viewport
 * point (`position`) or below-right of an anchor element (`anchor`), always
 * clamped inside the viewport with an 8px margin. Items are plain data
 * (`MenuItem[]`) so callers don't hand-roll markup; an optional header can
 * be projected via `<div menuHeader>…</div>`.
 *
 * Like `ModalComponent`, this teleports itself to `<body>` on init and
 * removes itself on destroy — the caller owns visibility (e.g. an `@if`)
 * and reacts to `closed`/`select` to tear it down.
 */
@Component({
  selector: 'app-menu',
  standalone: true,
  imports: [IconComponent],
  templateUrl: './menu.component.html',
  styleUrl: './menu.component.css',
})
export class MenuComponent implements OnInit, AfterViewInit, OnDestroy {
  /** Open at a fixed viewport point (e.g. a right-click). Ignored if `anchor` is set. */
  position = input<MenuPoint | null>(null);
  /** Open anchored below-right of this element (e.g. a "more actions" button). */
  anchor = input<HTMLElement | null>(null);
  items = input<MenuItem[]>([]);

  select = output<string>();
  closed = output<void>();

  private el = inject(ElementRef<HTMLElement>);

  @ViewChild('menuEl') private menuElRef!: ElementRef<HTMLElement>;
  @ViewChildren('itemBtn') private itemEls!: QueryList<ElementRef<HTMLButtonElement>>;

  left = signal(0);
  top = signal(0);
  activeIndex = signal(-1);

  ngOnInit() {
    document.body.appendChild(this.el.nativeElement);
  }

  ngAfterViewInit() {
    this.positionMenu();
    const first = this.enabledIndexes()[0] ?? -1;
    this.focusIndex(first);
  }

  ngOnDestroy() {
    this.el.nativeElement.remove();
  }

  private positionMenu() {
    const menuEl = this.menuElRef.nativeElement;
    const rect = menuEl.getBoundingClientRect();
    const margin = 8;
    const anchor = this.anchor();
    const point = this.position();
    let x: number;
    let y: number;
    if (anchor) {
      const a = anchor.getBoundingClientRect();
      x = a.right - rect.width;
      y = a.bottom + 4;
    } else if (point) {
      x = point.x;
      y = point.y;
    } else {
      x = margin;
      y = margin;
    }
    this.left.set(Math.max(margin, Math.min(x, window.innerWidth - rect.width - margin)));
    this.top.set(Math.max(margin, Math.min(y, window.innerHeight - rect.height - margin)));
  }

  private enabledIndexes(): number[] {
    return this.items()
      .map((it, i) => (it.disabled ? -1 : i))
      .filter(i => i >= 0);
  }

  private focusIndex(i: number) {
    if (i < 0) return;
    this.activeIndex.set(i);
    queueMicrotask(() => this.itemEls?.get(i)?.nativeElement.focus());
  }

  private moveFocus(delta: 1 | -1) {
    const enabled = this.enabledIndexes();
    if (!enabled.length) return;
    const cur = enabled.indexOf(this.activeIndex());
    const next = cur < 0 ? 0 : (cur + delta + enabled.length) % enabled.length;
    this.focusIndex(enabled[next]);
  }

  onKeydown(event: KeyboardEvent) {
    const enabled = this.enabledIndexes();
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        this.moveFocus(1);
        break;
      case 'ArrowUp':
        event.preventDefault();
        this.moveFocus(-1);
        break;
      case 'Home':
        event.preventDefault();
        this.focusIndex(enabled[0] ?? -1);
        break;
      case 'End':
        event.preventDefault();
        this.focusIndex(enabled[enabled.length - 1] ?? -1);
        break;
      case 'Escape':
        event.preventDefault();
        this.close();
        break;
      case 'Tab':
        this.close();
        break;
    }
  }

  pick(item: MenuItem) {
    if (item.disabled) return;
    this.select.emit(item.id);
    this.close();
  }

  onBackdropContext(event: MouseEvent) {
    event.preventDefault();
    this.close();
  }

  close() {
    this.closed.emit();
  }
}
