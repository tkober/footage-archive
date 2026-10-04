import {
  AfterViewInit,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  ViewChild,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

/**
 * Floating panel anchored to an element — used for forms (keyword/location/
 * list pickers) and the tasks popover. Opens below the anchor by default,
 * flips above it when there isn't enough room below, and is always clamped
 * horizontally inside the viewport (8px margin). Content is projected
 * as-is; closes on outside click or Esc.
 *
 * Teleports itself to `<body>` on init / removes itself on destroy, same
 * convention as `ModalComponent` and `MenuComponent`. The caller owns
 * visibility and reacts to `closed` to tear it down.
 */
@Component({
  selector: 'app-popover',
  standalone: true,
  templateUrl: './popover.component.html',
  styleUrl: './popover.component.css',
})
export class PopoverComponent implements OnInit, AfterViewInit, OnDestroy {
  anchor = input.required<HTMLElement>();
  /** Preferred width in px; the panel still clamps to the viewport. */
  width = input(320);

  closed = output<void>();

  private el = inject(ElementRef<HTMLElement>);
  @ViewChild('popEl') private popElRef!: ElementRef<HTMLElement>;

  left = signal(0);
  top = signal(0);

  ngOnInit() {
    document.body.appendChild(this.el.nativeElement);
  }

  ngAfterViewInit() {
    this.positionPopover();
    queueMicrotask(() => this.popElRef.nativeElement.focus());
  }

  ngOnDestroy() {
    this.el.nativeElement.remove();
  }

  private positionPopover() {
    const popEl = this.popElRef.nativeElement;
    const rect = popEl.getBoundingClientRect();
    const a = this.anchor().getBoundingClientRect();
    const margin = 8;

    const fitsBelow = a.bottom + 6 + rect.height <= window.innerHeight - margin;
    const top = fitsBelow ? a.bottom + 6 : Math.max(margin, a.top - 6 - rect.height);

    let left = a.right - rect.width;
    left = Math.max(margin, Math.min(left, window.innerWidth - rect.width - margin));

    this.top.set(top);
    this.left.set(left);
  }

  onBackdropClick() {
    this.closed.emit();
  }

  onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      this.closed.emit();
    }
  }
}
