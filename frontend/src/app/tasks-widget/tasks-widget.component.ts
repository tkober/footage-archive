import { DatePipe } from '@angular/common';
import { Component, computed, inject, Input, OnDestroy, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';

import { Task } from '../models';
import { ApiService } from '../services/api.service';

@Component({
  selector: 'app-tasks-widget',
  standalone: true,
  imports: [DatePipe, RouterLink],
  templateUrl: './tasks-widget.component.html',
  styleUrl: './tasks-widget.component.css',
})
export class TasksWidgetComponent implements OnInit, OnDestroy {
  @Input() pollIntervalMs = 5000;

  private api = inject(ApiService);
  private pollSub?: Subscription;
  private knownTaskStatus = new Map<string, Task['status']>();

  tasks = signal<Task[]>([]);
  open = signal(false);

  runningCount = computed(() => this.tasks().filter(t => t.status === 'RUNNING' || t.status === 'QUEUED').length);
  failedCount = computed(() => this.tasks().filter(t => t.status === 'FAILED').length);

  ngOnInit() {
    this.pollSub = timer(0, this.pollIntervalMs)
      .pipe(switchMap(() => this.api.getTasks()))
      .subscribe({ next: tasks => this.applyTasks(tasks) });

    this.api.taskRefresh$.subscribe(() => this.refresh());
  }

  ngOnDestroy() {
    this.pollSub?.unsubscribe();
  }

  private applyTasks(tasks: Task[]) {
    // A Rediscover task that just transitioned into COMPLETED may have left
    // open path conflicts behind — nudge the sidebar badge + the maintenance
    // page's conflicts section to reload.
    for (const task of tasks) {
      const previous = this.knownTaskStatus.get(task.id);
      if (task.status === 'COMPLETED' && previous !== 'COMPLETED' && task.name === 'Rediscover') {
        this.api.conflictsChanged$.next();
      }
      this.knownTaskStatus.set(task.id, task.status);
    }
    this.tasks.set(tasks);
  }

  /** Non-zero conflict count parsed out of a completed Rediscover task's
      summary ("3 relinked · 2 conflicts · 0 new (not tracked) · 1 unchanged"). */
  conflictCount(task: Task): number {
    if (task.name !== 'Rediscover' || task.status !== 'COMPLETED' || !task.progress) return 0;
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

  clearAll() {
    const ids = this.tasks().map(t => t.id);
    ids.forEach(id => this.api.deleteTask(id).subscribe({
      next: () => this.tasks.update(list => list.filter(t => t.id !== id)),
    }));
  }

  statusLabel(status: Task['status']): string {
    return { PENDING: 'Pending', QUEUED: 'Queued', RUNNING: 'Running', COMPLETED: 'Done', FAILED: 'Failed' }[status];
  }
}
