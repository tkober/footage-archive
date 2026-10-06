import { Component, ElementRef, Input, OnDestroy, OnInit, ViewChild, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Subscription, of, timer } from 'rxjs';
import { catchError, filter, switchMap } from 'rxjs/operators';

import { SystemDiagnosticsResponse } from '../models';
import { ApiService } from '../services/api.service';
import { PopoverComponent } from '../shared/popover/popover.component';

/** Degrees below the throttle limit at which the chip turns amber. */
const WARM_MARGIN_C = 10;

/** Header readout of host CPU usage + temperature (#71), next to the tasks
    widget so it stays visible while working in a folder. Polls
    `/system/diagnostics` at the task poll interval (skipped while the tab is
    hidden); the chip turns amber near the temperature limit and red while
    heavy jobs are throttled. Click opens a popover with the details. */
@Component({
  selector: 'app-system-widget',
  standalone: true,
  imports: [RouterLink, PopoverComponent],
  templateUrl: './system-widget.component.html',
  styleUrl: './system-widget.component.css',
})
export class SystemWidgetComponent implements OnInit, OnDestroy {
  @Input() pollIntervalMs = 5000;

  private api = inject(ApiService);
  private pollSub?: Subscription;

  @ViewChild('triggerBtn') triggerBtnRef?: ElementRef<HTMLButtonElement>;

  data = signal<SystemDiagnosticsResponse | null>(null);
  open = signal(false);

  state = computed<'ok' | 'warm' | 'throttled'>(() => {
    const d = this.data();
    if (!d) return 'ok';
    if (d.runtime.throttled) return 'throttled';
    const temp = d.runtime.cpu_temperature_c;
    const limit = d.settings.cpu_temp_limit_c;
    return temp != null && limit > 0 && temp >= limit - WARM_MARGIN_C ? 'warm' : 'ok';
  });

  cpuLabel = computed(() => {
    const usage = this.data()?.runtime.cpu_usage_percent;
    return usage == null ? '–' : `${Math.round(usage)}%`;
  });

  tempLabel = computed(() => {
    const temp = this.data()?.runtime.cpu_temperature_c;
    return temp == null ? '–' : `${Math.round(temp)}°`;
  });

  title = computed(() => {
    const d = this.data();
    if (!d) return 'System load';
    const parts = [`CPU ${this.cpuLabel()}`, `${this.tempLabel()}C`];
    if (d.runtime.throttled) parts.push(`paused: ${d.runtime.throttle_reason}`);
    return parts.join(' · ');
  });

  ngOnInit() {
    this.pollSub = timer(0, this.pollIntervalMs)
      .pipe(
        filter(() => typeof document === 'undefined' || !document.hidden),
        switchMap(() => this.api.getSystemDiagnostics().pipe(catchError(() => of(null)))),
      )
      .subscribe(data => { if (data) this.data.set(data); });
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

  loadLabel(): string {
    const load = this.data()?.runtime.load_avg;
    return load ? `${load.load_1m.toFixed(2)} / ${load.load_5m.toFixed(2)} / ${load.load_15m.toFixed(2)}` : '–';
  }

  tempLimitLabel(): string {
    const limit = this.data()?.settings.cpu_temp_limit_c ?? 0;
    return limit > 0 ? `limit ${limit}°C` : 'no limit';
  }

  panelWidth(): number {
    return typeof window !== 'undefined' && window.innerWidth <= 760 ? window.innerWidth - 16 : 300;
  }
}
