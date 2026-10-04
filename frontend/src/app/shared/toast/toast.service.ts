import { Injectable, signal } from '@angular/core';

export interface ToastAction {
  label: string;
  run: () => void;
}

export interface ToastOptions {
  action?: ToastAction;
  /** Auto-dismiss delay in ms. Default 4000. */
  duration?: number;
}

export interface Toast {
  id: number;
  message: string;
  action?: ToastAction;
}

const DEFAULT_DURATION_MS = 4000;

/**
 * App-wide toast notifications — bottom-left, stacked, inverse colors
 * (`--text` background / `--bg` text, per the design prototype's `.toast`).
 * `ToastOutletComponent` renders whatever this service holds; put it once
 * in `app.component.html`. Replaces ad hoc per-page "operation result"
 * banners (e.g. the browser's old `file-op-message`).
 */
@Injectable({ providedIn: 'root' })
export class ToastService {
  private nextId = 1;
  private timers = new Map<number, ReturnType<typeof setTimeout>>();

  toasts = signal<Toast[]>([]);

  show(message: string, options: ToastOptions = {}): number {
    const id = this.nextId++;
    this.toasts.update(list => [...list, { id, message, action: options.action }]);
    const duration = options.duration ?? DEFAULT_DURATION_MS;
    if (duration > 0) {
      this.timers.set(
        id,
        setTimeout(() => this.dismiss(id), duration),
      );
    }
    return id;
  }

  dismiss(id: number) {
    const timer = this.timers.get(id);
    if (timer) {
      clearTimeout(timer);
      this.timers.delete(id);
    }
    this.toasts.update(list => list.filter(t => t.id !== id));
  }

  runAction(toast: Toast) {
    toast.action?.run();
    this.dismiss(toast.id);
  }
}
