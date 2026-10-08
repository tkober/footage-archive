import { DatePipe } from '@angular/common';
import { Component, ElementRef, ViewChild, computed, inject, Input, OnDestroy, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Subscription, forkJoin, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';

import { ScanJob, ScanJobListEntry, ScanUnit, Task } from '../models';
import { ApiService } from '../services/api.service';
import { IconComponent } from '../shared/icon/icon.component';
import { PopoverComponent } from '../shared/popover/popover.component';

/** Tasks whose final summary reports path conflicts (see api/tracking.py) —
    also the set of tasks whose COMPLETED transition should make the
    browser reload its current directory (#134/#139): a scan/rediscover can
    change which files are tracked and a Census (#139) rewrites the
    DirectoryStats rows the folder-tile badges read. `conflictCount()` below
    simply finds no "N conflicts" text in a Census's progress and reports 0,
    so reusing this one set for both purposes is harmless. */
const CONFLICT_TASKS = new Set(['Rediscover', 'Scan directory', 'Track file', 'Census']);

/** Where a task/job is listed (#93/#140): actually working, waiting for its
    turn (queued, or running but blocked on shared capacity), or done. */
type TaskPhase = 'running' | 'waiting' | 'finished';

const PHASE_LABELS: Record<TaskPhase, string> = { running: 'Running', waiting: 'Waiting', finished: 'Finished' };

/** SVG ring geometry — r=11.5 like the prototype's `.ring`. */
const RING_RADIUS = 11.5;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

@Component({
  selector: 'app-tasks-widget',
  standalone: true,
  imports: [DatePipe, RouterLink, IconComponent, PopoverComponent],
  templateUrl: './tasks-widget.component.html',
  styleUrl: './tasks-widget.component.css',
})
export class TasksWidgetComponent implements OnInit, OnDestroy {
  @Input() pollIntervalMs = 5000;

  private api = inject(ApiService);
  private pollSub?: Subscription;
  private knownTaskStatus = new Map<string, Task['status']>();

  @ViewChild('triggerBtn') triggerBtnRef?: ElementRef<HTMLButtonElement>;

  readonly ringCircumference = RING_CIRCUMFERENCE;

  open = signal(false);

  /** Every task from `GET /tasks`, including the synthetic one-per-scan-job
      entries (#137) — kept around so `applyTasks`' status-transition
      detection (conflicts badge, browser reload, #134/#139) keeps working
      for scan jobs without its own code path. Never rendered directly. */
  private rawTasks = signal<Task[]>([]);

  /** `GET /scan-jobs` (#140), all jobs, newest first, minus PLANNED ones —
      a PLANNED job is an in-progress plan dialog, not a task. */
  jobs = signal<ScanJobListEntry[]>([]);

  /** Full unit list + live activity per job, fetched for every RUNNING or
      PAUSED job and every expanded one (#140). Dropped once a job no
      longer needs it (collapsed and not running/paused, or gone). */
  private jobDetails = signal<Map<string, ScanJob>>(new Map());

  /** Manual expand/collapse overrides, keyed by job id — collapsed by
      default for a finished job, expanded for a running one, otherwise
      whatever the user last clicked (#140). */
  private expandOverrides = signal<Map<string, boolean>>(new Map());

  /** Job ids whose unit list was expanded past the compact "+N more" cap (#140). */
  private fullUnitJobIds = signal<Set<string>>(new Set());

  private jobIds = computed(() => new Set(this.jobs().map(j => j.id)));
  /** `GET /tasks` entries actually rendered — the synthetic scan-job ones
      are hidden here since their job is rendered as its own row (#140). */
  tasks = computed(() => this.rawTasks().filter(t => !this.jobIds().has(t.id)));

  isEmpty = computed(() => this.tasks().length === 0 && this.jobs().length === 0);

  private runningTasks = computed(() => this.tasks().filter(t => t.status === 'RUNNING' || t.status === 'QUEUED'));
  private activeJobs = computed(() => this.jobs().filter(j => j.status === 'RUNNING' || j.status === 'QUEUED' || j.status === 'PAUSED'));
  runningCount = computed(() => this.runningTasks().length + this.activeJobs().length);
  failedCount = computed(() => this.tasks().filter(t => t.status === 'FAILED').length + this.jobs().filter(j => j.status === 'FAILED').length);
  activeCount = computed(() =>
    this.runningTasks().filter(t => this.phase(t) === 'running').length
    + this.activeJobs().filter(j => this.jobPhase(j) === 'running').length);
  waitingCount = computed(() => this.runningCount() - this.activeCount());

  /** Non-empty sections in display order: running, waiting, finished — each
      holding both plain tasks and scan jobs (#140). */
  groups = computed(() => (['running', 'waiting', 'finished'] as TaskPhase[])
    .map(phase => ({
      phase,
      label: PHASE_LABELS[phase],
      tasks: this.tasks().filter(t => this.phase(t) === phase),
      jobs: this.jobs().filter(j => this.jobPhase(j) === phase),
    }))
    .filter(g => g.tasks.length > 0 || g.jobs.length > 0));

  triggerTitle = computed(() => {
    const parts = ['Tasks'];
    if (this.activeCount()) parts.push(`${this.activeCount()} running`);
    if (this.waitingCount()) parts.push(`${this.waitingCount()} waiting`);
    if (this.failedCount()) parts.push(`${this.failedCount()} failed`);
    return parts.join(' · ');
  });

  /** Average fraction (0..1) across running tasks' "N / M" progress text and
      running jobs' units_done/units_total; `null` when neither has one,
      meaning the ring should spin indeterminately instead of showing a
      fixed sweep. */
  ringProgress = computed<number | null>(() => {
    const taskFractions = this.runningTasks()
      .map(t => this.parseProgressFraction(t.progress))
      .filter((f): f is number => f !== null);
    const jobFractions = this.jobs()
      .filter(j => j.status === 'RUNNING' && j.units_total > 0)
      .map(j => j.units_done / j.units_total);
    const fractions = [...taskFractions, ...jobFractions];
    if (!fractions.length) return null;
    return fractions.reduce((a, b) => a + b, 0) / fractions.length;
  });

  ringDashOffset = computed(() => {
    const p = this.ringProgress();
    return p === null ? RING_CIRCUMFERENCE * 0.75 : RING_CIRCUMFERENCE * (1 - p);
  });

  ngOnInit() {
    this.pollSub = timer(0, this.pollIntervalMs)
      .pipe(switchMap(() => forkJoin([this.api.getTasks(), this.api.getScanJobs()])))
      .subscribe({ next: ([tasks, jobs]) => { this.applyTasks(tasks); this.applyJobs(jobs); } });

    this.api.taskRefresh$.subscribe(() => this.refresh());
  }

  ngOnDestroy() {
    this.pollSub?.unsubscribe();
  }

  private parseProgressFraction(progress: string | null): number | null {
    if (!progress) return null;
    const match = progress.match(/(\d+)\s*\/\s*(\d+)/);
    if (!match) return null;
    const denominator = parseInt(match[2], 10);
    if (!denominator) return null;
    return parseInt(match[1], 10) / denominator;
  }

  /** Per-task progress-bar fraction (0..1), or `null` for an indeterminate bar. */
  taskProgressFraction(task: Task): number | null {
    return this.parseProgressFraction(task.progress);
  }

  private applyTasks(tasks: Task[]) {
    // A rediscover or scan task that just transitioned into COMPLETED may have left
    // open path conflicts behind — nudge the sidebar badge + the maintenance
    // page's conflicts section to reload. The same transition also means the
    // browser's untracked-badge counts (#134) may be stale, so it gets its
    // own notification to reload the directory it's showing. Runs over every
    // task including the synthetic scan-job ones (#137) — hiding them from
    // the rendered list (`tasks` computed) doesn't change this.
    for (const task of tasks) {
      const previous = this.knownTaskStatus.get(task.id);
      if (task.status === 'COMPLETED' && previous !== 'COMPLETED' && CONFLICT_TASKS.has(task.name)) {
        this.api.conflictsChanged$.next();
        this.api.taskCompleted$.next(task);
      }
      this.knownTaskStatus.set(task.id, task.status);
    }
    this.rawTasks.set(tasks);
  }

  private applyJobs(jobs: ScanJobListEntry[]) {
    this.jobs.set(jobs.filter(j => j.status !== 'PLANNED'));
    this.fetchNeededJobDetails();
  }

  private mergeJobDetail(job: ScanJob) {
    this.jobDetails.update(m => {
      const next = new Map(m);
      next.set(job.id, job);
      return next;
    });
  }

  /** Fetches `GET /scan-jobs/{id}` for every job that needs its unit list
      right now (RUNNING, PAUSED, or expanded), and drops any stored detail
      a job no longer needs — a handful of requests per poll tick at most. */
  private fetchNeededJobDetails() {
    const needed = this.jobs().filter(j => this.needsDetail(j));
    const neededIds = new Set(needed.map(j => j.id));
    this.jobDetails.update(m => {
      let changed = false;
      const next = new Map(m);
      for (const id of Array.from(next.keys())) {
        if (!neededIds.has(id)) { next.delete(id); changed = true; }
      }
      return changed ? next : m;
    });
    for (const job of needed) {
      this.api.getScanJob(job.id).subscribe({ next: detail => this.mergeJobDetail(detail) });
    }
  }

  private needsDetail(job: ScanJobListEntry): boolean {
    return job.status === 'RUNNING' || job.status === 'PAUSED' || this.isExpanded(job);
  }

  /** Non-conflict count parsed out of a completed rediscover/scan task's
      summary ("3 relinked · 2 conflicts · …"). */
  conflictCount(task: Task): number {
    if (!CONFLICT_TASKS.has(task.name) || task.status !== 'COMPLETED' || !task.progress) return 0;
    const match = task.progress.match(/(\d+)\s+conflicts?/);
    return match ? parseInt(match[1], 10) : 0;
  }

  /** Same parse as `conflictCount`, over a finished scan job's summary. */
  jobConflictCount(job: ScanJobListEntry): number {
    if (job.status !== 'DONE' || !job.summary) return 0;
    const match = job.summary.match(/(\d+)\s+conflicts?/);
    return match ? parseInt(match[1], 10) : 0;
  }

  toggle() {
    this.open.update(v => !v);
  }

  close() {
    this.open.set(false);
  }

  remove(task: Task) {
    this.api.deleteTask(task.id).subscribe({
      next: () => this.rawTasks.update(list => list.filter(t => t.id !== task.id)),
    });
  }

  refresh() {
    forkJoin([this.api.getTasks(), this.api.getScanJobs()]).subscribe({
      next: ([tasks, jobs]) => { this.applyTasks(tasks); this.applyJobs(jobs); },
    });
  }

  /** Removes COMPLETED tasks via the dedicated endpoint (which also clears
      DONE/CANCELLED scan jobs server-side, #137), and FAILED tasks/jobs
      (which that endpoint doesn't cover) one by one. Running/queued/pending
      ones are left alone — this clears only what's actually finished. */
  clearFinished() {
    const failedTaskIds = this.rawTasks().filter(t => t.status === 'FAILED').map(t => t.id);
    const failedJobs = this.jobs().filter(j => j.status === 'FAILED');
    this.api.clearCompletedTasks().subscribe({
      next: () => {
        this.rawTasks.update(list => list.filter(t => t.status !== 'COMPLETED'));
        this.jobs.update(list => list.filter(j => j.status !== 'DONE' && j.status !== 'CANCELLED'));
      },
    });
    failedTaskIds.forEach(id => this.api.deleteTask(id).subscribe({
      next: () => this.rawTasks.update(list => list.filter(t => t.id !== id)),
    }));
    failedJobs.forEach(job => this.api.deleteScanJob(job.id).subscribe({
      next: () => this.jobs.update(list => list.filter(j => j.id !== job.id)),
    }));
  }

  hasFinished(): boolean {
    return this.rawTasks().some(t => t.status === 'COMPLETED' || t.status === 'FAILED')
      || this.jobs().some(j => j.status === 'DONE' || j.status === 'FAILED' || j.status === 'CANCELLED');
  }

  /** 440px — wide enough for a task's description path to wrap readably —
      but full-width with 8px margins on phones. */
  panelWidth(): number {
    return typeof window !== 'undefined' && window.innerWidth <= 760 ? window.innerWidth - 16 : 440;
  }

  /** Splits a description so each `/` stays at the end of its segment,
      letting the template insert a `<wbr>` after every slash — the text
      wraps at directory boundaries instead of mid-path. */
  descriptionSegments(description: string): string[] {
    return description.split(/(?<=\/)/);
  }

  phase(task: Task): TaskPhase {
    if (task.status === 'COMPLETED' || task.status === 'FAILED') return 'finished';
    if (task.status === 'RUNNING' && (!task.activity || task.activity === 'ACTIVE')) return 'running';
    return 'waiting';
  }

  statusLabel(task: Task): string {
    if (task.status === 'RUNNING') {
      switch (task.activity) {
        case 'WAITING_WORKER': return 'Waiting for a free worker';
        case 'WAITING_HEAVY': return 'Waiting for a preview slot';
        case 'THROTTLED': return 'Paused · host too hot or busy';
        default: return 'Running';
      }
    }
    return { PENDING: 'Pending', QUEUED: 'Queued', RUNNING: 'Running', COMPLETED: 'Done', FAILED: 'Failed' }[task.status];
  }

  // ── Scan jobs (#140) ──

  jobPhase(job: ScanJobListEntry): TaskPhase {
    if (job.status === 'RUNNING') return 'running';
    if (job.status === 'QUEUED' || job.status === 'PAUSED') return 'waiting';
    return 'finished';
  }

  jobStatusLabel(job: ScanJobListEntry): string {
    if (job.status === 'PAUSED') return 'Paused';
    return ({ QUEUED: 'Queued', RUNNING: 'Running', DONE: 'Done', FAILED: 'Failed', CANCELLED: 'Cancelled' } as Record<string, string>)[job.status] ?? job.status;
  }

  jobName(job: ScanJobListEntry): string {
    const name = job.root_path.split('/').filter(Boolean).pop();
    return `Scan ${name ?? job.root_path}`;
  }

  jobDescription(job: ScanJobListEntry): string {
    return `Scanning directory "${job.root_path}".`;
  }

  jobProgressFraction(job: ScanJobListEntry): number | null {
    return job.units_total > 0 ? job.units_done / job.units_total : null;
  }

  jobCountsLine(job: ScanJobListEntry): string {
    return `${job.units_done} / ${job.units_total} folder${job.units_total === 1 ? '' : 's'} · `
      + `${job.files_done} / ${job.files_total} file${job.files_total === 1 ? '' : 's'}`;
  }

  jobUnits(job: ScanJobListEntry): ScanUnit[] {
    return this.jobDetails().get(job.id)?.units ?? [];
  }

  /** Whether this job's unit list is worth rendering at all — a 0-unit job
      (e.g. an empty-tree `scan-directory` compatibility call) has nothing
      to expand. */
  hasUnits(job: ScanJobListEntry): boolean {
    return job.units_total > 0;
  }

  isExpanded(job: ScanJobListEntry): boolean {
    const override = this.expandOverrides().get(job.id);
    if (override !== undefined) return override;
    return job.status === 'RUNNING';
  }

  toggleExpand(job: ScanJobListEntry) {
    const next = !this.isExpanded(job);
    this.expandOverrides.update(m => {
      const copy = new Map(m);
      copy.set(job.id, next);
      return copy;
    });
    if (next) this.fetchNeededJobDetails();
  }

  /** Compact view (#140): running units, failed units, and the next three
      waiting ones; everything else collapses into "+N more" until the user
      expands the full list for this job. */
  visibleUnits(job: ScanJobListEntry, units: ScanUnit[]): { shown: ScanUnit[]; moreCount: number } {
    if (this.fullUnitJobIds().has(job.id)) return { shown: units, moreCount: 0 };
    const keep = new Set(units.filter(u => u.status === 'RUNNING' || u.status === 'FAILED').map(u => u.id));
    units.filter(u => u.status === 'QUEUED' || u.status === 'PLANNED').slice(0, 3).forEach(u => keep.add(u.id));
    const shown = units.filter(u => keep.has(u.id));
    return { shown, moreCount: units.length - shown.length };
  }

  showAllUnits(job: ScanJobListEntry) {
    this.fullUnitJobIds.update(s => new Set(s).add(job.id));
  }

  unitRelativePath(job: ScanJobListEntry, unit: ScanUnit): string {
    const root = job.root_path;
    if (unit.directory === root) return '.';
    return unit.directory.startsWith(root) ? unit.directory.slice(root.length).replace(/^\/+/, '') : unit.directory;
  }

  unitPillLabel(unit: ScanUnit): string {
    return ({
      PLANNED: 'waiting', QUEUED: 'waiting', RUNNING: 'running', DONE: 'done',
      FAILED: 'failed', CANCELLED: 'cancelled', DESELECTED: 'deselected',
    } as Record<string, string>)[unit.status];
  }

  unitPillClass(unit: ScanUnit): string {
    return ({
      PLANNED: 'pill-faint', QUEUED: 'pill-faint', RUNNING: 'pill-accent', DONE: 'pill-ok',
      FAILED: 'pill-danger', CANCELLED: 'pill-faint', DESELECTED: 'pill-faint',
    } as Record<string, string>)[unit.status];
  }

  /** `progress` while running, `error` once failed — same convention as a
      plain task's status line. */
  unitStatusText(unit: ScanUnit): string | null {
    if (unit.status === 'RUNNING') return unit.progress;
    if (unit.status === 'FAILED') return unit.error;
    return null;
  }

  /** "31 / 40" parsed out of `progress` when present, else the planned file count. */
  unitCounterText(unit: ScanUnit): string {
    const match = unit.progress?.match(/(\d+)\s*\/\s*(\d+)/);
    if (match) return `${match[1]} / ${match[2]}`;
    if (unit.media_file_count != null) return `${unit.media_file_count} file${unit.media_file_count === 1 ? '' : 's'}`;
    return '';
  }

  pauseJob(job: ScanJobListEntry) {
    this.api.pauseScanJob(job.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }

  resumeJob(job: ScanJobListEntry) {
    this.api.resumeScanJob(job.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }

  cancelJob(job: ScanJobListEntry) {
    this.api.cancelScanJob(job.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }

  /** Per-item remove for a FAILED job — the bulk "Clear finished" doesn't
      cover these, same asymmetry as a FAILED task. */
  removeJob(job: ScanJobListEntry) {
    this.api.deleteScanJob(job.id).subscribe({ next: () => this.jobs.update(list => list.filter(j => j.id !== job.id)) });
  }

  cancelUnit(job: ScanJobListEntry, unit: ScanUnit) {
    this.api.cancelUnit(job.id, unit.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }

  retryUnit(job: ScanJobListEntry, unit: ScanUnit) {
    this.api.retryUnit(job.id, unit.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }

  moveUnitToFront(job: ScanJobListEntry, unit: ScanUnit) {
    this.api.moveUnitTop(job.id, unit.id).subscribe({ next: detail => { this.mergeJobDetail(detail); this.refresh(); } });
  }
}
