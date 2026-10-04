import { AfterViewInit, Component, ElementRef, computed, input, output, signal, viewChild } from '@angular/core';

import { ModalComponent } from '../../modal/modal.component';

/**
 * Confirm dialog for irreversible actions: the confirm button only enables
 * once the user has typed `confirmWord` (case-insensitive). Use the plain
 * ConfirmDialogComponent for everything that can be undone.
 */
@Component({
  selector: 'app-type-to-confirm-dialog',
  standalone: true,
  imports: [ModalComponent],
  templateUrl: './type-to-confirm-dialog.component.html',
  styleUrl: './type-to-confirm-dialog.component.css',
})
export class TypeToConfirmDialogComponent implements AfterViewInit {
  title = input('Confirm');
  message = input('');
  confirmWord = input('delete');
  confirmLabel = input('Delete');
  busy = input(false);
  error = input<string | null>(null);

  confirmed = output<void>();
  cancelled = output<void>();

  typed = signal('');
  matches = computed(() => this.typed().trim().toLowerCase() === this.confirmWord().toLowerCase());

  private inputEl = viewChild<ElementRef<HTMLInputElement>>('wordInput');

  ngAfterViewInit() {
    this.inputEl()?.nativeElement.focus();
  }

  onInput(event: Event) {
    this.typed.set((event.target as HTMLInputElement).value);
  }

  onCancel() {
    if (!this.busy()) this.cancelled.emit();
  }

  onConfirm() {
    if (this.matches() && !this.busy()) this.confirmed.emit();
  }
}
