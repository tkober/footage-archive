import { DatePipe } from '@angular/common';
import { Component, ElementRef, ViewChild, computed, inject, Input, OnDestroy, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';

import { Task } from '../models';
import { ApiService } from '../services/api.service';
import { IconComponent } from '../shared/icon/icon.component';
import { PopoverComponent } from '../shared/popover/popover.component';

/** Tasks whose final summary reports path conflicts (see api/tracking.py). */
const CONFLICT_TASKS = new Set(['Rediscover', 'Scan directory', 'Track file']);

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

  tasks = signal<Task[]>([]);
  open = signal(false);

  runningTasks = computed(() => this.tasks().filter(t => t.status === 'RUNNING' || t.status === 'QUEUED'));
  runningCount = computed(() => this.runningTasks().length);
  failedCount = computed(() => this.tasks().filter(t => t.status === 'FAILED').length);

  /** Average fraction (0..1) of running tasks whose `progress` text contains
      an "N / M" count; `null` when none do, meaning the ring should spin
      indeterminately instead of showing a fixed sweep. */
  ringProgress = computed<number | null>(() => {
    const fractions = this.runningTasks()
      .map(t => this.parseProgressFraction(t.progress))
      .filter((f): f is number => f !== null);
    if (!fractions.length) return null;
    return fractions.reduce((a, b) => a + b, 0) / fractions.length;
  });

  ringDashOffset = computed(() => {
    const p = this.ringProgress();
    return p === null ? RING_CIRCUMFERENCE * 0.75 : RING_CIRCUMFERENCE * (1 - p);
  });

  ngOnInit() {
    this.pollSub = timer(0, this.pollIntervalMs)
      .pipe(switchMap(() => this.api.getTasks()))
      .subscribe({ next: tasks => this.applyTasks(tasks) });

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
    // page's conflicts section to reload.
    for (const task of tasks) {
      const previous = this.knownTaskStatus.get(task.id);
      if (task.status === 'COMPLETED' && previous !== 'COMPLETED' && CONFLICT_TASKS.has(task.name)) {
        this.api.conflictsChanged$.next();
      }
      this.knownTaskStatus.set(task.id, task.status);
    }
    this.tasks.set(tasks);
  }

  /** Non-zero conflict count parsed out of a completed rediscover/scan task's
      summary ("3 relinked · 2 conflicts · …"). */
  conflictCount(task: Task): number {
    if (!CONFLICT_TASKS.has(task.name) || task.status !== 'COMPLETED' || !task.progress) return 0;
    const match = task.progress.match(/(\d+)\s+conflicts?/);
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
      next: () => this.tasks.update(list => list.filter(t => t.id !== task.id)),
    });
  }

  refresh() {
    this.api.getTasks().subscribe({ next: tasks => this.applyTasks(tasks) });
  }

  /** Removes COMPLETED tasks via the dedicated endpoint, and FAILED ones
      (which that endpoint doesn't cover) one by one. Running/queued/pending
      tasks are left alone — this clears only what's actually finished. */
  clearFinished() {
    const failedIds = this.tasks().filter(t => t.status === 'FAILED').map(t => t.id);
    this.api.clearCompletedTasks().subscribe({
      next: () => this.tasks.update(list => list.filter(t => t.status !== 'COMPLETED')),
    });
    failedIds.forEach(id => this.api.deleteTask(id).subscribe({
      next: () => this.tasks.update(list => list.filter(t => t.id !== id)),
    }));
  }

  hasFinished(): boolean {
    return this.tasks().some(t => t.status === 'COMPLETED' || t.status === 'FAILED');
  }

  /** 340px like the prototype, but full-width with 8px margins on phones. */
  panelWidth(): number {
    return typeof window !== 'undefined' && window.innerWidth <= 760 ? window.innerWidth - 16 : 340;
  }

  statusLabel(status: Task['status']): string {
    return { PENDING: 'Pending', QUEUED: 'Queued', RUNNING: 'Running', COMPLETED: 'Done', FAILED: 'Failed' }[status];
  }
}
