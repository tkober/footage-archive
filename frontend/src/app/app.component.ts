import { Component, computed, inject, OnInit, signal } from '@angular/core';
import { NavigationEnd, NavigationStart, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';

import { ApiService } from './services/api.service';
import { ThemeService } from './services/theme.service';
import { HeaderService, HeaderCrumb } from './services/header.service';
import { SystemWidgetComponent } from './system-widget/system-widget.component';
import { TasksWidgetComponent } from './tasks-widget/tasks-widget.component';
import { QuickJumpComponent } from './shared/quick-jump/quick-jump.component';
import { ToastOutletComponent } from './shared/toast/toast-outlet.component';
import { MenuComponent, MenuItem } from './shared/menu/menu.component';
import { IconComponent } from './shared/icon/icon.component';
import { VersionFlyoutComponent } from './version-flyout/version-flyout.component';

/** Rail / bottom-tab-bar entries, in display order. `extra` marks the ones
    that disappear from the mobile tab bar into the "More" menu. */
interface NavEntry {
  path: string;
  label: string;
  icon: string;
  extra?: boolean;
}

const NAV_ENTRIES: NavEntry[] = [
  { path: '/browser', label: 'Browse', icon: 'grid' },
  { path: '/search', label: 'Search', icon: 'search' },
  { path: '/lists', label: 'Lists', icon: 'list' },
  { path: '/map', label: 'Map', icon: 'map' },
  { path: '/maintenance', label: 'Health', icon: 'wrench', extra: true },
  { path: '/settings', label: 'Settings', icon: 'cog', extra: true },
];

/** "More" menu id of the Version entry — not a route, opens the flyout. */
const VERSION_MENU_ID = 'version';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    RouterOutlet, RouterLink, RouterLinkActive,
    SystemWidgetComponent, TasksWidgetComponent, QuickJumpComponent, ToastOutletComponent,
    MenuComponent, IconComponent, VersionFlyoutComponent,
  ],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css'
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);
  private header = inject(HeaderService);
  // Injected (unused directly here) so the theme is applied/kept in sync as soon as the app
  // bootstraps, not only once Settings is opened.
  private theme = inject(ThemeService);

  readonly navEntries = NAV_ENTRIES;
  readonly mainNavEntries = NAV_ENTRIES.filter(e => !e.extra);
  readonly moreNavEntries = NAV_ENTRIES.filter(e => e.extra);

  moreOpen = signal(false);
  moreAnchor = signal<HTMLElement | null>(null);

  pageTitle = signal('Footage Archive');
  taskPollIntervalMs = signal(5000);
  versionOpen = signal(false);
  versionAnchor = signal<HTMLElement | null>(null);
  versionSide = signal<'below' | 'right'>('right');
  conflictsCount = signal(0);

  /** What the topbar actually renders: the page's own breadcrumbs when it
      published any, otherwise a single "current page" crumb from the
      route title. */
  crumbs = computed<HeaderCrumb[]>(() => this.header.crumbs() ?? [{ label: this.pageTitle() }]);

  constructor(private router: Router) {}

  ngOnInit() {
    // Clear before the next page's component (re)runs ngOnInit, so a page that
    // doesn't publish its own crumbs falls back to the route title instead of
    // showing the previous page's trail for a moment. The route isn't resolved
    // yet at NavigationStart, so the title itself is set below, on NavigationEnd.
    this.router.events.pipe(
      filter(e => e instanceof NavigationStart)
    ).subscribe(() => this.header.clear());

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

    this.refreshConflictsCount();
    this.api.conflictsChanged$.subscribe(() => this.refreshConflictsCount());
  }

  private refreshConflictsCount() {
    this.api.getConflictsCount().subscribe({
      next: res => this.conflictsCount.set(res.count),
      error: () => {},
    });
  }

  openMore(anchor: HTMLElement) {
    this.moreAnchor.set(anchor);
    this.moreOpen.set(true);
  }

  closeMore() {
    this.moreOpen.set(false);
  }

  moreMenuItems(): MenuItem[] {
    return [
      ...this.moreNavEntries.map(e => ({ id: e.path, label: e.label, icon: e.icon })),
      { id: VERSION_MENU_ID, label: 'Version', icon: 'info' },
    ];
  }

  onMoreSelect(id: string) {
    if (id === VERSION_MENU_ID) {
      this.openVersion(this.moreAnchor()!, 'below');
      return;
    }
    this.router.navigateByUrl(id);
  }

  openVersion(anchor: HTMLElement, side: 'below' | 'right') {
    this.versionAnchor.set(anchor);
    this.versionSide.set(side);
    this.versionOpen.set(true);
  }

  closeVersion() {
    this.versionOpen.set(false);
  }
}
