import { Component, HostListener, input, output } from '@angular/core';

import { ModalComponent } from '../../modal/modal.component';

/**
 * Generic confirm/cancel dialog built on ModalComponent. Reusable wherever a
 * destructive or non-trivial action needs a yes/no gate (list delete, item
 * removal, and future callers like the detail panel).
 */
@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [ModalComponent],
  templateUrl: './confirm-dialog.component.html',
  styleUrl: './confirm-dialog.component.css',
})
export class ConfirmDialogComponent {
  title = input('Confirm');
  message = input('');
  confirmLabel = input('Remove');
  danger = input(false);

  confirmed = output<void>();
  cancelled = output<void>();

  @HostListener('document:keydown.enter', ['$event'])
  onEnter(event: Event) {
    const tag = (event.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    this.confirmed.emit();
  }

  onCancel() {
    this.cancelled.emit();
  }

  onConfirm() {
    this.confirmed.emit();
  }
}
