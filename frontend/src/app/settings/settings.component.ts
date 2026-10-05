import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { Subscription, of, timer } from 'rxjs';
import { catchError, switchMap } from 'rxjs/operators';

import { SystemDiagnosticsResponse } from '../models';
import { ApiService } from '../services/api.service';
import { ThemeChoice, ThemeService } from '../services/theme.service';

const DIAGNOSTICS_POLL_MS = 5000;

@Component({
  selector: 'app-settings',
  standalone: true,
  templateUrl: './settings.component.html',
  styleUrl: './settings.component.css'
})
export class SettingsComponent implements OnInit, OnDestroy {
  private themeService = inject(ThemeService);
  private api = inject(ApiService);
  private pollSub?: Subscription;

  choice = this.themeService.choice;
  diagnostics = signal<SystemDiagnosticsResponse | null>(null);

  readonly options: { value: ThemeChoice; label: string }[] = [
    { value: 'system', label: 'System' },
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
  ];

  /** Settings displayed in the "Performance" section's read-only key/value
      list, in display order. */
  readonly settingsRows: { label: string; key: keyof SystemDiagnosticsResponse['settings'] }[] = [
    { label: 'Worker pool size', key: 'worker_pool_size' },
    { label: 'Heavy job concurrency', key: 'heavy_job_concurrency' },
    { label: 'FFmpeg threads', key: 'ffmpeg_threads' },
    { label: 'Process niceness', key: 'process_niceness' },
    { label: 'CPU temperature limit', key: 'cpu_temp_limit_c' },
    { label: 'Load average limit', key: 'load_avg_limit' },
    { label: 'DB pool size', key: 'db_pool_size' },
    { label: 'DB max overflow', key: 'db_max_overflow' },
  ];

  select(choice: ThemeChoice) {
    this.themeService.setChoice(choice);
  }

  ngOnInit() {
    this.pollSub = timer(0, DIAGNOSTICS_POLL_MS)
      .pipe(switchMap(() => this.api.getSystemDiagnostics().pipe(catchError(() => of(null)))))
      .subscribe({ next: data => { if (data) this.diagnostics.set(data); } });
  }

  ngOnDestroy() {
    this.pollSub?.unsubscribe();
  }

  settingValue(key: keyof SystemDiagnosticsResponse['settings']): string {
    const settings = this.diagnostics()?.settings;
    if (!settings) return '—';
    const value = settings[key];
    if (key === 'cpu_temp_limit_c') return value > 0 ? `${value}°C` : 'disabled';
    if (key === 'load_avg_limit') return value > 0 ? `${value}` : 'disabled';
    return `${value}`;
  }

  cpuTemperatureLabel(): string {
    const temp = this.diagnostics()?.runtime.cpu_temperature_c;
    return temp === null || temp === undefined ? 'no sensor' : `${temp.toFixed(1)}°C`;
  }

  loadAvgLabel(): string {
    const load = this.diagnostics()?.runtime.load_avg;
    if (!load) return '—';
    return `${load.load_1m.toFixed(2)} / ${load.load_5m.toFixed(2)} / ${load.load_15m.toFixed(2)}`;
  }

  cpuLabel(): string {
    const runtime = this.diagnostics()?.runtime;
    if (!runtime) return '—';
    return runtime.cpu_limit != null
      ? `${runtime.cpu_count} (cgroup limit ${runtime.cpu_limit})`
      : `${runtime.cpu_count}`;
  }
}
