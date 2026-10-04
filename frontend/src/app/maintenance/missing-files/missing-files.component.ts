import { Component, OnInit, computed, inject, signal } from '@angular/core';

import { ApiService } from '../../services/api.service';
import { RediscoverDialogComponent } from '../../shared/rediscover-dialog/rediscover-dialog.component';
import { TypeToConfirmDialogComponent } from '../../shared/type-to-confirm-dialog/type-to-confirm-dialog.component';
import { MissingFile } from '../../models';

export interface MissingFileGroup {
  directory: string;
  relativeDirectory: string;
  files: MissingFile[];
}

@Component({
  selector: 'app-missing-files',
  standalone: true,
  imports: [RediscoverDialogComponent, TypeToConfirmDialogComponent],
  templateUrl: './missing-files.component.html',
  styleUrl: './missing-files.component.css',
})
export class MissingFilesComponent implements OnInit {
  readonly api = inject(ApiService);

  loading = signal(false);
  hasChecked = signal(false);
  error = signal<string | null>(null);
  files = signal<MissingFile[]>([]);
  rootDir = signal('');

  rediscoverStartDir = signal<string | null>(null);
  showRediscover = signal(false);
  rediscoverNote = signal<string | null>(null);

  /** Files queued for permanent removal; null = dialog closed. */
  pendingRemove = signal<{ label: string; files: MissingFile[] } | null>(null);
  removing = signal(false);
  removeError = signal<string | null>(null);
  removeNote = signal<string | null>(null);

  removeMessage = computed(() => {
    const pending = this.pendingRemove();
    if (!pending) return '';
    const n = pending.files.length;
    const attached = pending.files.filter(f => f.keyword_count > 0 || f.has_location || f.list_count > 0).length;
    let msg = `${n} missing file${n === 1 ? '' : 's'} in ${pending.label} will be removed from the archive, ` +
      'including keywords, location, list memberships and previews.';
    if (attached > 0) {
      msg += ` ${attached} of them ${attached === 1 ? 'has' : 'have'} keywords, a location or list entries.`;
    }
    return msg + ' This cannot be undone. Nothing on disk is touched.';
  });

  groups = computed<MissingFileGroup[]>(() => {
    const root = this.rootDir();
    const byDirectory = new Map<string, MissingFile[]>();
    for (const file of this.files()) {
      const bucket = byDirectory.get(file.directory);
      if (bucket) bucket.push(file);
      else byDirectory.set(file.directory, [file]);
    }
    return Array.from(byDirectory.entries()).map(([directory, files]) => ({
      directory,
      relativeDirectory: this.relativize(directory, root),
      files,
    }));
  });

  ngOnInit(): void {
    this.api.getConfig().subscribe(cfg => this.rootDir.set(cfg.root_dir));
    this.check();
  }

  check(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getMissingFiles().subscribe({
      next: files => {
        this.files.set(files);
        this.loading.set(false);
        this.hasChecked.set(true);
      },
      error: () => {
        this.error.set('Failed to check for missing files.');
        this.loading.set(false);
        this.hasChecked.set(true);
      },
    });
  }

  openRediscover(group: MissingFileGroup): void {
    this.rediscoverStartDir.set(group.directory);
    this.showRediscover.set(true);
  }

  closeRediscover(): void {
    this.showRediscover.set(false);
  }

  onRediscoverStarted(): void {
    this.showRediscover.set(false);
    this.rediscoverNote.set('Rediscover started — see tasks.');
  }

  openRemove(group: MissingFileGroup): void {
    this.openRemoveFor(group.relativeDirectory, group.files);
  }

  openRemoveAll(): void {
    this.openRemoveFor('all folders', this.files());
  }

  private openRemoveFor(label: string, files: MissingFile[]): void {
    this.removeError.set(null);
    this.pendingRemove.set({ label, files });
  }

  cancelRemove(): void {
    this.pendingRemove.set(null);
  }

  confirmRemove(): void {
    const pending = this.pendingRemove();
    if (!pending) return;
    this.removing.set(true);
    this.removeError.set(null);
    this.api.removeMissingFiles(pending.files.map(f => f.md5_hash)).subscribe({
      next: res => {
        this.removing.set(false);
        this.pendingRemove.set(null);
        let note = `Removed ${res.removed} file${res.removed === 1 ? '' : 's'}.`;
        if (res.skipped > 0) {
          note += ` ${res.skipped} skipped (found on disk again or no longer tracked).`;
        }
        this.removeNote.set(note);
        this.check();
      },
      error: () => {
        this.removing.set(false);
        this.removeError.set('Failed to remove files.');
      },
    });
  }

  private relativize(directory: string, root: string): string {
    if (root && directory.startsWith(root)) {
      const rel = directory.slice(root.length).replace(/^\/+/, '');
      return rel || '/';
    }
    return directory;
  }
}
