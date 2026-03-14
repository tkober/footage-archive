import { Component, computed, inject, OnInit, signal } from '@angular/core';

import { ApiService } from '../services/api.service';
import { PathChild } from '../models';

const PAGE_SIZE = 50;

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
  total = signal(0);
  loading = signal(false);
  loadingMore = signal(false);
  error = signal<string | null>(null);
  private page = 1;

  hasMore = computed(() => this.entries().length < this.total());

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
    this.page = 1;

    this.api.listDirectory({ path, page: 1, page_size: PAGE_SIZE }).subscribe({
      next: response => {
        this.entries.set(response.items);
        this.total.set(response.total);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load directory');
        this.loading.set(false);
      }
    });
  }

  loadMore() {
    const path = this.currentPath();
    if (!path || this.loadingMore()) return;

    this.loadingMore.set(true);
    this.page++;

    this.api.listDirectory({ path, page: this.page, page_size: PAGE_SIZE }).subscribe({
      next: response => {
        this.entries.update(existing => [...existing, ...response.items]);
        this.total.set(response.total);
        this.loadingMore.set(false);
      },
      error: () => {
        this.page--;
        this.loadingMore.set(false);
      }
    });
  }

  onEntryClick(entry: PathChild) {
    if (entry.type === 'directory') {
      this.navigateTo(entry.path);
    }
  }
}
