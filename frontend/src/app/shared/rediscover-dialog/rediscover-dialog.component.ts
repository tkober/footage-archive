import { Component, inject, input, OnInit, output, signal } from '@angular/core';

import { ModalComponent } from '../../modal/modal.component';
import { FolderPickerComponent } from '../folder-picker/folder-picker.component';
import { ApiService } from '../../services/api.service';

/**
 * Starts a POST /tracking/rediscover. Two entry shapes, controlled by
 * whether `path` is given:
 *  - `path` omitted (missing-files group header): first a folder picker
 *    ("Rediscover…" / "Rediscover here") lets the user choose WHERE to
 *    look, then the checkbox step.
 *  - `path` given (browser context menu on a directory): the folder is
 *    already known, so only the checkbox step is shown.
 */
@Component({
  selector: 'app-rediscover-dialog',
  standalone: true,
  imports: [ModalComponent, FolderPickerComponent],
  templateUrl: './rediscover-dialog.component.html',
  styleUrl: './rediscover-dialog.component.css',
})
export class RediscoverDialogComponent implements OnInit {
  private api = inject(ApiService);

  rootDir = input.required<string>();
  /** The folder to rediscover. Omit to let the user pick one first. */
  path = input<string | null>(null);
  /** Initial directory for the folder picker step (only used when `path` is omitted). */
  startDir = input<string | null>(null);

  started = output<void>();
  cancelled = output<void>();

  step = signal<'pick' | 'confirm'>('pick');
  targetPath = signal<string>('');
  trackNew = signal(false);
  starting = signal(false);
  error = signal<string | null>(null);

  ngOnInit() {
    const path = this.path();
    if (path) {
      this.targetPath.set(path);
      this.step.set('confirm');
    }
  }

  relativeTarget(): string {
    const root = this.rootDir();
    const target = this.targetPath();
    if (!root || !target) return target;
    if (target === root) return '/';
    return target.startsWith(root) ? target.slice(root.length).replace(/^\/+/, '') : target;
  }

  onFolderPicked(path: string) {
    this.targetPath.set(path);
    this.step.set('confirm');
  }

  onPickerCancelled() {
    this.cancelled.emit();
  }

  toggleTrackNew() {
    this.trackNew.update(v => !v);
  }

  start() {
    this.starting.set(true);
    this.error.set(null);
    this.api.rediscover(this.targetPath(), this.trackNew()).subscribe({
      next: () => {
        this.api.taskRefresh$.next();
        this.started.emit();
      },
      error: err => {
        this.starting.set(false);
        this.error.set(err.error?.detail ?? 'Failed to start rediscover');
      },
    });
  }

  onCancel() {
    this.cancelled.emit();
  }
}
