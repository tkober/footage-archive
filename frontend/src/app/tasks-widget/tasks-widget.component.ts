import { DatePipe } from '@angular/common';
import { Component, computed, inject, Input, OnDestroy, OnInit, signal } from '@angular/core';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';

import { Task } from '../models';
import { ApiService } from '../services/api.service';

@Component({
  selector: 'app-tasks-widget',
  standalone: true,
  imports: [DatePipe],
  templateUrl: './tasks-widget.component.html',
  styleUrl: './tasks-widget.component.css',
})
export class TasksWidgetComponent implements OnInit, OnDestroy {
  @Input() pollIntervalMs = 5000;

  private api = inject(ApiService);
  private pollSub?: Subscription;

  tasks = signal<Task[]>([]);
  open = signal(false);

  runningCount = computed(() => this.tasks().filter(t => t.status === 'RUNNING' || t.status === 'QUEUED').length);
  failedCount = computed(() => this.tasks().filter(t => t.status === 'FAILED').length);

  ngOnInit() {
    this.pollSub = timer(0, this.pollIntervalMs)
      .pipe(switchMap(() => this.api.getTasks()))
      .subscribe({ next: tasks => this.tasks.set(tasks) });
  }

  ngOnDestroy() {
    this.pollSub?.unsubscribe();
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
    this.api.getTasks().subscribe({ next: tasks => this.tasks.set(tasks) });
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
