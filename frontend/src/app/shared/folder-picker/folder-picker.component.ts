import { Component, computed, ElementRef, inject, input, OnInit, output, signal, ViewChild } from '@angular/core';

import { ModalComponent } from '../../modal/modal.component';
import { ApiService } from '../../services/api.service';
import { PathChild } from '../../models';

/**
 * "Move to…" folder navigator on top of ModalComponent. Browses directories
 * from ROOT_DIR (reusing the existing directory-listing API, directories
 * only), with breadcrumbs and inline "New folder" creation. The source
 * directory itself, any of its descendants, and the sources' current common
 * parent (a no-op move) are disabled as a target.
 */
@Component({
  selector: 'app-folder-picker',
  standalone: true,
  imports: [ModalComponent],
  templateUrl: './folder-picker.component.html',
  styleUrl: './folder-picker.component.css',
})
export class FolderPickerComponent implements OnInit {
  private api = inject(ApiService);

  // ── Inputs / Outputs ──
  rootDir = input.required<string>();
  /** Absolute paths being moved — used to block invalid targets. */
  sourcePaths = input.required<string[]>();
  /** Directory to open in initially; defaults to the common parent of the sources. */
  startDir = input<string | null>(null);

  picked = output<string>();
  cancelled = output<void>();

  // ── Internal state ──
  currentDir = signal('');
  dirs = signal<PathChild[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);

  creatingFolder = signal(false);
  newFolderName = signal('');
  newFolderError = signal<string | null>(null);

  @ViewChild('newFolderInput') newFolderInputRef?: ElementRef<HTMLInputElement>;

  breadcrumbs = computed(() => {
    const root = this.rootDir();
    const current = this.currentDir();
    if (!root || !current) return [];
    const rootParts = root.split('/').filter(Boolean);
    const currentParts = current.split('/').filter(Boolean);
    return currentParts.slice(rootParts.length - 1).map((label, i) => ({
      label,
      path: '/' + currentParts.slice(0, rootParts.length - 1 + i + 1).join('/'),
    }));
  });

  private sourceParents = computed(() => new Set(this.sourcePaths().map(p => this.parentOf(p))));

  /** Why "Move here" is disabled for the currently open directory, or null if it's a valid target. */
  blockReason = computed<string | null>(() => {
    const dir = this.currentDir();
    if (!dir) return null;
    for (const s of this.sourcePaths()) {
      if (dir === s) return 'Cannot move into itself';
      if (dir.startsWith(s + '/')) return 'Cannot move into its own subfolder';
    }
    if (this.sourceParents().size === 1 && dir === [...this.sourceParents()][0]) {
      return 'Already there';
    }
    return null;
  });

  ngOnInit() {
    const start = this.startDir() ?? this.parentOf(this.sourcePaths()[0] ?? this.rootDir());
    this.navigateTo(start || this.rootDir());
  }

  /** True when `path` is the source itself or a descendant of it (can't navigate a source into itself). */
  isBlocked(path: string): boolean {
    for (const s of this.sourcePaths()) {
      if (path === s || path.startsWith(s + '/')) return true;
    }
    return false;
  }

  relativeDir(dir: string): string {
    const root = this.rootDir();
    if (!dir) return '';
    if (dir === root) return '/';
    return dir.startsWith(root) ? dir.slice(root.length).replace(/^\/+/, '') : dir;
  }

  navigateTo(path: string) {
    this.loading.set(true);
    this.error.set(null);
    this.currentDir.set(path);
    this.cancelNewFolder();
    this.api.listDirectory({ path, page: 1, page_size: 500, sort_by: 'name', dirs_first: true }).subscribe({
      next: resp => {
        this.dirs.set(resp.items.filter(e => e.type === 'directory'));
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load folders');
        this.loading.set(false);
      },
    });
  }

  startNewFolder() {
    this.creatingFolder.set(true);
    this.newFolderName.set('');
    this.newFolderError.set(null);
    setTimeout(() => this.newFolderInputRef?.nativeElement.focus());
  }

  cancelNewFolder() {
    this.creatingFolder.set(false);
    this.newFolderName.set('');
    this.newFolderError.set(null);
  }

  submitNewFolder() {
    const name = this.newFolderName().trim();
    if (!name) return;
    this.newFolderError.set(null);
    this.api.mkdir(this.currentDir(), name).subscribe({
      next: resp => this.navigateTo(resp.path),
      error: err => this.newFolderError.set(err.error?.detail ?? 'Failed to create folder'),
    });
  }

  confirmPick() {
    if (this.blockReason()) return;
    this.picked.emit(this.currentDir());
  }

  onCancel() {
    this.cancelled.emit();
  }

  private parentOf(path: string): string {
    const idx = path.lastIndexOf('/');
    return idx > 0 ? path.slice(0, idx) : '/';
  }
}
