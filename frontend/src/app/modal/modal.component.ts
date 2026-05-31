import { Component, ElementRef, EventEmitter, HostListener, Input, OnDestroy, OnInit, Output, inject } from '@angular/core';

@Component({
  selector: 'app-modal',
  standalone: true,
  templateUrl: './modal.component.html',
  styleUrl: './modal.component.css'
})
export class ModalComponent implements OnInit, OnDestroy {
  @Input() title = '';
  @Input() wide = false;
  @Output() close = new EventEmitter<void>();

  private el = inject(ElementRef<HTMLElement>);

  // Teleport to <body> so the fixed backdrop escapes any transformed/clipping
  // ancestor (e.g. the sliding .detail-view) and covers the whole viewport.
  ngOnInit() {
    document.body.appendChild(this.el.nativeElement);
  }

  ngOnDestroy() {
    this.el.nativeElement.remove();
  }

  @HostListener('document:keydown.escape')
  onEscape() {
    this.close.emit();
  }

  onBackdropClick() {
    this.close.emit();
  }
}
