import { Component, OnInit, inject, signal } from '@angular/core';

import { ApiService } from '../../services/api.service';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog/confirm-dialog.component';
import { ConflictEntry, ResolveBatchStrategy } from '../../models';

@Component({
  selector: 'app-path-conflicts',
  standalone: true,
  imports: [ConfirmDialogComponent],
  templateUrl: './path-conflicts.component.html',
  styleUrl: './path-conflicts.component.css',
})
export class PathConflictsComponent implements OnInit {
  readonly api = inject(ApiService);

  loading = signal(false);
  error = signal<string | null>(null);
  conflicts = signal<ConflictEntry[]>([]);
  rootDir = signal('');

  /** md5_hash -> currently-selected path (radio state), defaults to tracked_path. */
  selection = signal<Record<string, string>>({});
  applying = signal<Record<string, boolean>>({});
  applyError = signal<Record<string, string>>({});

  pendingBatch = signal<ResolveBatchStrategy | null>(null);
  batchRunning = signal(false);
  batchResult = signal<string | null>(null);

  ngOnInit(): void {
    this.api.getConfig().subscribe(cfg => this.rootDir.set(cfg.root_dir));
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getConflicts().subscribe({
      next: entries => {
        this.conflicts.set(entries);
        const sel: Record<string, string> = {};
        for (const e of entries) sel[e.md5_hash] = e.tracked_path;
        this.selection.set(sel);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load path conflicts.');
        this.loading.set(false);
      },
    });
  }

  relative(path: string): string {
    const root = this.rootDir();
    if (root && path.startsWith(root)) {
      const rel = path.slice(root.length).replace(/^\/+/, '');
      return rel || '/';
    }
    return path;
  }

  select(md5Hash: string, path: string): void {
    this.selection.update(sel => ({ ...sel, [md5Hash]: path }));
  }

  apply(entry: ConflictEntry): void {
    const chosen = this.selection()[entry.md5_hash] ?? entry.tracked_path;
    this.applying.update(a => ({ ...a, [entry.md5_hash]: true }));
    this.applyError.update(e => ({ ...e, [entry.md5_hash]: '' }));
    this.api.resolveConflict(entry.md5_hash, chosen).subscribe({
      next: () => {
        this.conflicts.update(list => list.filter(e => e.md5_hash !== entry.md5_hash));
        this.applying.update(a => ({ ...a, [entry.md5_hash]: false }));
        this.api.conflictsChanged$.next();
      },
      error: err => {
        this.applying.update(a => ({ ...a, [entry.md5_hash]: false }));
        this.applyError.update(e => ({ ...e, [entry.md5_hash]: err.error?.detail ?? 'Failed to resolve' }));
      },
    });
  }

  openBatch(strategy: ResolveBatchStrategy): void {
    this.pendingBatch.set(strategy);
  }

  cancelBatch(): void {
    this.pendingBatch.set(null);
  }

  batchTitle(): string {
    return this.pendingBatch() === 'keep_tracked' ? 'Keep all current' : 'Use new location for all';
  }

  batchMessage(): string {
    const count = this.conflicts().length;
    return this.pendingBatch() === 'keep_tracked'
      ? `Resolve all ${count} conflict${count === 1 ? '' : 's'} by keeping their currently tracked path. The other copy stays on disk and will be untracked afterwards. Nothing is deleted.`
      : `Resolve all ${count} conflict${count === 1 ? '' : 's'} by switching to their new location, where unambiguous. The other copy stays on disk and will be untracked afterwards. Nothing is deleted.`;
  }

  confirmBatch(): void {
    const strategy = this.pendingBatch();
    if (!strategy) return;
    const hashes = this.conflicts().map(e => e.md5_hash);
    this.batchRunning.set(true);
    this.pendingBatch.set(null);
    this.api.resolveConflictsBatch(strategy, hashes).subscribe({
      next: res => {
        this.batchRunning.set(false);
        const skipLabel = res.skipped.length > 0 ? ` · ${res.skipped.length} skipped (${this.summarizeSkips(res.skipped)})` : '';
        this.batchResult.set(`${res.resolved} resolved${skipLabel}`);
        this.api.conflictsChanged$.next();
        this.load();
      },
      error: () => {
        this.batchRunning.set(false);
        this.batchResult.set('Batch resolve failed.');
      },
    });
  }

  private summarizeSkips(skipped: { reason: string }[]): string {
    const counts = new Map<string, number>();
    for (const s of skipped) counts.set(s.reason, (counts.get(s.reason) ?? 0) + 1);
    return [...counts.entries()].map(([reason, n]) => `${n} ${reason.toLowerCase()}`).join(', ');
  }
}
