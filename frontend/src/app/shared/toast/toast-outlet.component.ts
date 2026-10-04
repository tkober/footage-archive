import { Component, inject } from '@angular/core';

import { IconComponent } from '../icon/icon.component';
import { Toast, ToastService } from './toast.service';

/** Renders whatever `ToastService` holds. One instance lives in `app.component.html`. */
@Component({
  selector: 'app-toast-outlet',
  standalone: true,
  imports: [IconComponent],
  templateUrl: './toast-outlet.component.html',
  styleUrl: './toast-outlet.component.css',
})
export class ToastOutletComponent {
  toastService = inject(ToastService);

  runAction(toast: Toast) {
    this.toastService.runAction(toast);
  }

  dismiss(id: number) {
    this.toastService.dismiss(id);
  }
}
