import { Component, computed, inject, input, OnInit, output, signal } from '@angular/core';

import { ModalComponent } from '../../modal/modal.component';
import { IconComponent } from '../icon/icon.component';
import { ApiService } from '../../services/api.service';
import { ScanJob, ScanPlan, ScanUnit } from '../../models';

/**
 * Plan dialog (#140, epic #133) — opened instead of starting a folder scan
 * directly, from the folder context menu's "Scan folder…" and the toolbar's
 * "Scan folder" button. Plans a `POST /tracking/scan-plan` on open (and
 * again on every options change — planning is cheap), lets the user drop/
 * restore/reorder units, then starts the job or discards it.
 *
 * Every unit action (deselect/reselect/move-top) goes straight to the API
 * and the dialog re-renders from the returned job — there is no local
 * draft. Closing the dialog any other way than "Start" (Esc, backdrop, ✕,
 * "Discard") always discards the PLANNED job so none are left behind
 * before the backend's own 24h cleanup.
 */
@Component({
  selector: 'app-scan-plan-dialog',
  standalone: true,
  imports: [ModalComponent, IconComponent],
  templateUrl: './scan-plan-dialog.component.html',
  styleUrl: './scan-plan-dialog.component.css',
})
export class ScanPlanDialogComponent implements OnInit {
  private api = inject(ApiService);

  rootDir = input.required<string>();
  /** The folder to plan a scan for. */
  path = input.required<string>();

  started = output<void>();
  cancelled = output<void>();

  plan = signal<ScanPlan | null>(null);
  loading = signal(false);
  starting = signal(false);
  error = signal<string | null>(null);

  previews = signal(true);
  forceRehash = signal(false);
  onlyUntracked = signal(false);

  /** Non-deselected units — what "N folders · M files · K already tracked"
      and "Start N folders" both count. */
  activeUnits = computed<ScanUnit[]>(() => (this.plan()?.units ?? []).filter(u => u.status !== 'DESELECTED'));
  folderCount = computed(() => this.activeUnits().length);
  fileCount = computed(() => this.activeUnits().reduce((sum, u) => sum + (u.media_file_count ?? 0), 0));
  trackedCount = computed(() => this.activeUnits().reduce((sum, u) => sum + (u.tracked_file_count ?? 0), 0));

  /** Folder name shown in the dialog title — the last path segment, or "."
      when planning the root itself. */
  rootName = computed(() => {
    const path = this.path();
    const root = this.rootDir();
    if (path === root) return '.';
    return path.split('/').filter(Boolean).pop() ?? path;
  });

  ngOnInit() {
    this.requestPlan();
  }

  private requestPlan() {
    this.loading.set(true);
    this.error.set(null);
    this.api.createScanPlan(this.path(), {
      generateClipPreview: this.previews(),
      forceRehash: this.forceRehash(),
      onlyUntracked: this.onlyUntracked(),
    }).subscribe({
      next: plan => { this.plan.set(plan); this.loading.set(false); },
      error: () => { this.loading.set(false); this.error.set("Couldn't plan this scan."); },
    });
  }

  /** Changing an option discards the current plan and plans again —
      deselections made so far are lost (the plan is cheap to redo, ~0.1s
      for a tree the size of japan_2024). */
  private replan() {
    const current = this.plan();
    this.plan.set(null);
    if (current) {
      this.api.deleteScanJob(current.id).subscribe({
        next: () => this.requestPlan(),
        error: () => this.requestPlan(),
      });
    } else {
      this.requestPlan();
    }
  }

  setPreviews(value: boolean) {
    this.previews.set(value);
    this.replan();
  }

  setForceRehash(value: boolean) {
    this.forceRehash.set(value);
    this.replan();
  }

  setOnlyUntracked(value: boolean) {
    this.onlyUntracked.set(value);
    this.replan();
  }

  /** Directory relative to the plan's root — "." for the root itself. */
  relativeUnitPath(unit: ScanUnit): string {
    const root = this.plan()?.root_path ?? '';
    if (unit.directory === root) return '.';
    return unit.directory.startsWith(root) ? unit.directory.slice(root.length).replace(/^\/+/, '') : unit.directory;
  }

  unitPillLabel(unit: ScanUnit): string {
    return unit.status === 'DESELECTED' ? 'deselected' : 'planned';
  }

  deselect(unit: ScanUnit) {
    const plan = this.plan();
    if (!plan) return;
    this.api.deselectUnit(plan.id, unit.id).subscribe({ next: job => this.mergeJob(job) });
  }

  reselect(unit: ScanUnit) {
    const plan = this.plan();
    if (!plan) return;
    this.api.reselectUnit(plan.id, unit.id).subscribe({ next: job => this.mergeJob(job) });
  }

  moveToTop(unit: ScanUnit) {
    const plan = this.plan();
    if (!plan) return;
    this.api.moveUnitTop(plan.id, unit.id).subscribe({ next: job => this.mergeJob(job) });
  }

  /** Unit actions return a plain `ScanJobDto`, which has no `skipped` field —
      keep the plan's own `skipped` list, which never changes after the
      initial plan response. */
  private mergeJob(job: ScanJob) {
    const skipped = this.plan()?.skipped ?? [];
    this.plan.set({ ...job, skipped });
  }

  start() {
    const plan = this.plan();
    if (!plan || this.folderCount() === 0) return;
    this.starting.set(true);
    this.error.set(null);
    this.api.startScanJob(plan.id).subscribe({
      next: () => { this.starting.set(false); this.started.emit(); },
      error: () => { this.starting.set(false); this.error.set("Couldn't start the scan."); },
    });
  }

  /** Discard button, the empty-plan "Close" button, and the modal's own
      close paths (Esc/backdrop/✕) all land here — a PLANNED job is never
      left behind just because the dialog was dismissed. */
  discardAndClose() {
    const plan = this.plan();
    if (plan) this.api.deleteScanJob(plan.id).subscribe();
    this.cancelled.emit();
  }
}
