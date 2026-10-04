import { Component, inject, OnInit, signal } from '@angular/core';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';

import { ApiService } from './services/api.service';
import { ThemeService } from './services/theme.service';
import { TasksWidgetComponent } from './tasks-widget/tasks-widget.component';
import { QuickJumpComponent } from './shared/quick-jump/quick-jump.component';
import { ToastOutletComponent } from './shared/toast/toast-outlet.component';
import { APP_VERSION } from '../version';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, TasksWidgetComponent, QuickJumpComponent, ToastOutletComponent],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css'
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);
  // Injected (unused directly here) so the theme is applied/kept in sync as soon as the app
  // bootstraps, not only once Settings is opened.
  private theme = inject(ThemeService);

  sidebarOpen = true;
  pageTitle = signal('Footage Archive');
  taskPollIntervalMs = signal(5000);
  frontendVersion = APP_VERSION;
  backendVersion = signal('…');
  conflictsCount = signal(0);

  constructor(private router: Router) {}

  ngOnInit() {
    this.router.events.pipe(
      filter(e => e instanceof NavigationEnd)
    ).subscribe(() => {
      let route = this.router.routerState.snapshot.root;
      while (route.firstChild) route = route.firstChild;
      this.pageTitle.set(route.title ?? 'Footage Archive');
    });

    this.api.getConfig().subscribe({
      next: config => this.taskPollIntervalMs.set(config.task_poll_interval_ms),
    });

    this.api.getBackendVersion().subscribe({
      next: res => this.backendVersion.set(res.version),
      error: () => this.backendVersion.set('unknown'),
    });

    this.refreshConflictsCount();
    this.api.conflictsChanged$.subscribe(() => this.refreshConflictsCount());
  }

  private refreshConflictsCount() {
    this.api.getConflictsCount().subscribe({
      next: res => this.conflictsCount.set(res.count),
      error: () => {},
    });
  }

  toggleSidebar() {
    this.sidebarOpen = !this.sidebarOpen;
  }
}
