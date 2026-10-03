import { Component, OnInit, computed, inject, signal } from '@angular/core';

import { ApiService } from '../../services/api.service';
import { RediscoverDialogComponent } from '../../shared/rediscover-dialog/rediscover-dialog.component';
import { MissingFile } from '../../models';

export interface MissingFileGroup {
  directory: string;
  relativeDirectory: string;
  files: MissingFile[];
}

@Component({
  selector: 'app-missing-files',
  standalone: true,
  imports: [RediscoverDialogComponent],
  templateUrl: './missing-files.component.html',
  styleUrl: './missing-files.component.css',
})
export class MissingFilesComponent implements OnInit {
  readonly api = inject(ApiService);

  loading = signal(false);
  hasChecked = signal(false);
  error = signal<string | null>(null);
  files = signal<MissingFile[]>([]);
  rootDir = signal('');

  rediscoverStartDir = signal<string | null>(null);
  showRediscover = signal(false);
  rediscoverNote = signal<string | null>(null);

  groups = computed<MissingFileGroup[]>(() => {
    const root = this.rootDir();
    const byDirectory = new Map<string, MissingFile[]>();
    for (const file of this.files()) {
      const bucket = byDirectory.get(file.directory);
      if (bucket) bucket.push(file);
      else byDirectory.set(file.directory, [file]);
    }
    return Array.from(byDirectory.entries()).map(([directory, files]) => ({
      directory,
      relativeDirectory: this.relativize(directory, root),
      files,
    }));
  });

  ngOnInit(): void {
    this.api.getConfig().subscribe(cfg => this.rootDir.set(cfg.root_dir));
    this.check();
  }

  check(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getMissingFiles().subscribe({
      next: files => {
        this.files.set(files);
        this.loading.set(false);
        this.hasChecked.set(true);
      },
      error: () => {
        this.error.set('Failed to check for missing files.');
        this.loading.set(false);
        this.hasChecked.set(true);
      },
    });
  }

  openRediscover(group: MissingFileGroup): void {
    this.rediscoverStartDir.set(group.directory);
    this.showRediscover.set(true);
  }

  closeRediscover(): void {
    this.showRediscover.set(false);
  }

  onRediscoverStarted(): void {
    this.showRediscover.set(false);
    this.rediscoverNote.set('Rediscover started — see tasks.');
  }

  private relativize(directory: string, root: string): string {
    if (root && directory.startsWith(root)) {
      const rel = directory.slice(root.length).replace(/^\/+/, '');
      return rel || '/';
    }
    return directory;
  }
}
