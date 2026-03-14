import { Component, computed, inject, OnInit, signal } from '@angular/core';

import { ApiService } from '../services/api.service';
import { PathChild } from '../models';

@Component({
  selector: 'app-browser',
  standalone: true,
  templateUrl: './browser.component.html',
  styleUrl: './browser.component.css'
})
export class BrowserComponent implements OnInit {
  private api = inject(ApiService);

  rootDir = signal<string | null>(null);
  currentPath = signal<string | null>(null);
  entries = signal<PathChild[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);

  breadcrumbs = computed(() => {
    const root = this.rootDir();
    const current = this.currentPath();
    if (!root || !current) return [];

    const rootParts = root.split('/').filter(Boolean);
    const currentParts = current.split('/').filter(Boolean);

    return currentParts.slice(rootParts.length - 1).map((label, i) => ({
      label,
      path: '/' + currentParts.slice(0, rootParts.length - 1 + i + 1).join('/')
    }));
  });

  ngOnInit() {
    this.api.getConfig().subscribe({
      next: config => {
        this.rootDir.set(config.root_dir);
        this.navigateTo(config.root_dir);
      },
      error: () => this.error.set('Failed to load configuration')
    });
  }

  navigateTo(path: string) {
    this.loading.set(true);
    this.error.set(null);
    this.currentPath.set(path);

    this.api.listDirectory({ path }).subscribe({
      next: response => {
        this.entries.set(response.items);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load directory');
        this.loading.set(false);
      }
    });
  }

  onEntryClick(entry: PathChild) {
    if (entry.type === 'directory') {
      this.navigateTo(entry.path);
    }
  }
}
