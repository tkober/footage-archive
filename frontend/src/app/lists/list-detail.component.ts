import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { ApiService } from '../services/api.service';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { FileInfo, FileList, ListItem } from '../models';

const PAGE_SIZE = 500;

@Component({
  selector: 'app-list-detail',
  standalone: true,
  imports: [FormsModule, RouterLink, FileDetailPanelComponent, ConfirmDialogComponent],
  templateUrl: './list-detail.component.html',
  styleUrl: './list-detail.component.css',
})
export class ListDetailComponent implements OnInit {
  readonly api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  listId!: number;
  list = signal<FileList | null>(null);
  listNotFound = signal(false);
  rootDir = signal('');

  items = signal<ListItem[]>([]);
  total = signal(0);
  page = signal(1);
  loading = signal(false);

  selectedItem = signal<ListItem | null>(null);
  selectedFile = signal<FileInfo | null>(null);
  loadingDetails = signal(false);

  codeInput = signal('');
  codeError = signal<string | null>(null);

  pendingRemove = signal<ListItem | null>(null);

  ngOnInit(): void {
    this.listId = Number(this.route.snapshot.paramMap.get('id'));

    this.api.getConfig().subscribe(cfg => this.rootDir.set(cfg.root_dir));

    this.loadListMeta();
    this.loadItems(1, false);

    const code = this.route.snapshot.queryParamMap.get('code');
    if (code) this.jumpToCode(code, true);
  }

  private loadListMeta(): void {
    this.api.getLists().subscribe({
      next: lists => {
        const found = lists.find(l => l.id === this.listId) ?? null;
        this.list.set(found);
        this.listNotFound.set(!found);
      },
    });
  }

  private loadItems(page: number, append: boolean): void {
    this.loading.set(true);
    this.api.getListItems(this.listId, page, PAGE_SIZE).subscribe({
      next: resp => {
        this.total.set(resp.total);
        this.page.set(resp.page);
        this.items.set(append ? [...this.items(), ...resp.items] : resp.items);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  loadMore(): void {
    this.loadItems(this.page() + 1, true);
  }

  // ── Path display ──

  relativePath(item: ListItem): string {
    const root = this.rootDir();
    let dir = item.directory;
    if (root && dir.startsWith(root)) {
      dir = dir.slice(root.length).replace(/^\/+/, '');
    }
    return dir ? `${dir}/${item.file_name}` : item.file_name;
  }

  // ── Code jump ──

  submitCodeJump(): void {
    const code = this.codeInput().trim();
    if (!code) return;
    this.jumpToCode(code, false);
  }

  private jumpToCode(code: string, fromDeepLink: boolean): void {
    this.codeError.set(null);
    this.api.getListItemByCode(this.listId, code).subscribe({
      next: item => {
        this.openItem(item, fromDeepLink);
      },
      error: () => {
        this.codeError.set(`No item with code ${code.toUpperCase()} in this list`);
      },
    });
  }

  // ── Detail panel ──

  openItem(item: ListItem, replaceUrl = false): void {
    this.selectedItem.set(item);
    this.selectedFile.set(null);
    this.loadingDetails.set(true);
    this.codeError.set(null);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { code: item.item_code },
      replaceUrl,
    });
    const path = item.directory + '/' + item.file_name;
    this.api.getFileDetails(path).subscribe({
      next: info => { this.selectedFile.set(info); this.loadingDetails.set(false); },
      error: () => this.loadingDetails.set(false),
    });
  }

  closeDetail(): void {
    this.selectedItem.set(null);
    this.selectedFile.set(null);
    this.loadingDetails.set(false);
    this.router.navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl: true });
  }

  // ── Remove from list ──

  requestRemove(item: ListItem, event: Event): void {
    event.stopPropagation();
    this.pendingRemove.set(item);
  }

  cancelRemove(): void {
    this.pendingRemove.set(null);
  }

  confirmRemove(): void {
    const item = this.pendingRemove();
    if (!item) return;
    this.api.removeFileFromList(this.listId, item.md5_hash).subscribe({
      next: () => {
        this.items.update(list => list.filter(i => i.md5_hash !== item.md5_hash));
        this.total.update(t => Math.max(0, t - 1));
        this.list.update(l => l ? { ...l, item_count: Math.max(0, l.item_count - 1) } : l);
        if (this.selectedItem()?.md5_hash === item.md5_hash) this.closeDetail();
        this.pendingRemove.set(null);
      },
      error: () => this.pendingRemove.set(null),
    });
  }
}
