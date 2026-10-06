import { Component, OnInit, computed, inject, input, output, signal } from '@angular/core';

import { ApiService } from '../services/api.service';
import { PopoverComponent } from '../shared/popover/popover.component';
import { IconComponent } from '../shared/icon/icon.component';
import { ToastService } from '../shared/toast/toast.service';
import { APP_VERSION } from '../../version';

/** Version flyout (#95), opened from the rail's "Version" item (or the mobile
    "More" menu). Shows the frontend bundle's own version next to the backend's
    live `GET /version` (re-fetched every time it opens, so a freshly deployed
    backend shows up without a reload) and flags a mismatch between the two. */
@Component({
  selector: 'app-version-flyout',
  standalone: true,
  imports: [PopoverComponent, IconComponent],
  templateUrl: './version-flyout.component.html',
  styleUrl: './version-flyout.component.css',
})
export class VersionFlyoutComponent implements OnInit {
  anchor = input.required<HTMLElement>();
  side = input<'below' | 'right'>('right');

  closed = output<void>();

  private api = inject(ApiService);
  private toast = inject(ToastService);

  readonly frontendVersion = APP_VERSION;
  /** `undefined` while loading, `null` when the backend couldn't be reached. */
  backendVersion = signal<string | null | undefined>(undefined);

  state = computed<'loading' | 'ok' | 'mismatch' | 'unreachable'>(() => {
    const be = this.backendVersion();
    if (be === undefined) return 'loading';
    if (be === null) return 'unreachable';
    return be === this.frontendVersion ? 'ok' : 'mismatch';
  });

  ngOnInit() {
    this.api.getBackendVersion().subscribe({
      next: res => this.backendVersion.set(res.version),
      error: () => this.backendVersion.set(null),
    });
  }

  copy() {
    const be = this.backendVersion() ?? 'unreachable';
    const text = `Footage Archive · Frontend ${this.frontendVersion} · Backend ${be}`;
    navigator.clipboard?.writeText(text).then(
      () => this.toast.show('Version info copied.'),
      () => this.toast.show('Could not copy to clipboard.'),
    );
  }
}
