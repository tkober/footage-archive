import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { switchMap } from 'rxjs/operators';

import { Task } from '../models';
import { ApiService } from './api.service';

/**
 * Polls `GET /tasks/{id}` every `task_poll_interval_ms` (from `/config`)
 * until a background task reaches COMPLETED or FAILED (#64). Used after
 * starting a rescan to know when it's safe to cache-bust the regenerated
 * preview and reload file details — the tasks widget already shows live
 * progress/result text, this is just the completion signal for the caller.
 */
@Injectable({ providedIn: 'root' })
export class TaskPollService {
  private api = inject(ApiService);

  pollUntilDone(taskId: string): Observable<Task> {
    return this.api.getConfig().pipe(
      switchMap(config => new Observable<Task>(subscriber => {
        const intervalMs = config.task_poll_interval_ms;
        let stopped = false;
        let timer: ReturnType<typeof setTimeout> | undefined;

        const poll = () => {
          this.api.getTask(taskId).subscribe({
            next: task => {
              if (stopped) return;
              if (task.status === 'COMPLETED' || task.status === 'FAILED') {
                subscriber.next(task);
                subscriber.complete();
              } else {
                timer = setTimeout(poll, intervalMs);
              }
            },
            error: err => { if (!stopped) subscriber.error(err); },
          });
        };
        poll();

        return () => { stopped = true; clearTimeout(timer); };
      }))
    );
  }
}
