import { Injectable, signal } from '@angular/core';

export type ThemeChoice = 'system' | 'dark' | 'light';

const STORAGE_KEY = 'fa-theme';

/**
 * Applies the dark/light theme chosen in Settings.
 *
 * `choice` is the user's preference (persisted to localStorage); `resolved`
 * is the actual theme in effect once "system" is resolved via
 * matchMedia('(prefers-color-scheme: light)'). The resolved theme is
 * reflected onto <html data-theme> (and color-scheme), matched by the same
 * inline script in index.html that runs before first paint to avoid a
 * flash of the wrong theme.
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly choice = signal<ThemeChoice>(this.readStored());
  readonly resolved = signal<'dark' | 'light'>('dark');

  private media: MediaQueryList | null = null;
  private readonly onMediaChange = () => this.apply();

  constructor() {
    try {
      this.media = window.matchMedia('(prefers-color-scheme: light)');
      this.media.addEventListener('change', this.onMediaChange);
    } catch {
      this.media = null;
    }
    this.apply();
  }

  setChoice(choice: ThemeChoice): void {
    this.choice.set(choice);
    try {
      localStorage.setItem(STORAGE_KEY, choice);
    } catch {
      /* storage unavailable (private mode, quota, ...) — theme still applies this session */
    }
    this.apply();
  }

  private readStored(): ThemeChoice {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw === 'system' || raw === 'dark' || raw === 'light') return raw;
    } catch {
      /* storage unavailable — fall through to default */
    }
    return 'dark';
  }

  private apply(): void {
    const choice = this.choice();
    const resolved = choice === 'system' ? (this.systemPrefersLight() ? 'light' : 'dark') : choice;
    this.resolved.set(resolved);
    const root = document.documentElement;
    if (resolved === 'light') {
      root.setAttribute('data-theme', 'light');
    } else {
      root.setAttribute('data-theme', 'dark');
    }
    root.style.colorScheme = resolved;
  }

  private systemPrefersLight(): boolean {
    try {
      return this.media ? this.media.matches : false;
    } catch {
      return false;
    }
  }
}
