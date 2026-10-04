import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { combineLatest } from 'rxjs';

import { ApiService } from '../services/api.service';
import { FileDetailPanelComponent } from '../shared/file-detail-panel/file-detail-panel.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { MediaCardComponent, MediaCardKind } from '../shared/media-card/media-card.component';
import { FileInfo, FileList, ListItem, VIDEO_TYPES } from '../models';

const PAGE_SIZE = 500;

@Component({
  selector: 'app-list-detail',
  standalone: true,
  imports: [FormsModule, RouterLink, FileDetailPanelComponent, ConfirmDialogComponent, MediaCardComponent],
  templateUrl: './list-detail.component.html',
  styleUrl: './list-detail.component.css',
})
export class ListDetailComponent implements OnInit {
  readonly api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  listId = NaN; // set from the route on the first paramMap emission
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

  /** The code currently being looked up (request in flight), so the second of
      two combineLatest emissions for the same navigation doesn't fire a
      duplicate GET .../by-code request before selectedItem() is set. */
  private pendingCode: string | null = null;

  ngOnInit(): void {
    this.api.getConfig().subscribe(cfg => this.rootDir.set(cfg.root_dir));

    // The component is reused (not re-created) when navigating between two
    // /lists/:id routes — e.g. a detail-panel pill for another list, while
    // already on a list-detail page. Subscribe instead of reading the route
    // snapshot once, so both a changed id (reload the list) and a changed
    // code (open that item) are picked up. Guard re-opening on the code
    // actually differing from what's already open, since this component's
    // own navigate() calls (openItem/closeDetail) also flow back through here.
    //
    // paramMap and queryParamMap are separate observables that don't always
    // emit atomically for a single navigation (e.g. a pill linking to
    // /lists/:otherId?code=X can fire the queryParamMap update before the
    // paramMap update lands), so each callback re-reads both from the route's
    // snapshot — which Angular always updates as one unit — instead of
    // trusting the pair of values the two streams happened to emit together.
    combineLatest([this.route.paramMap, this.route.queryParamMap]).subscribe(() => {
      const id = Number(this.route.snapshot.paramMap.get('id'));
      if (id !== this.listId) {
        this.listId = id;
        this.list.set(null);
        this.listNotFound.set(false);
        this.items.set([]);
        this.total.set(0);
        this.page.set(1);
        this.selectedItem.set(null);
        this.selectedFile.set(null);
        this.loadingDetails.set(false);
        this.codeInput.set('');
        this.codeError.set(null);
        this.pendingCode = null;
        this.loadListMeta();
        this.loadItems(1, false);
      }

      const code = this.route.snapshot.queryParamMap.get('code');
      if (code && this.selectedItem()?.item_code !== code && this.pendingCode !== code) {
        this.pendingCode = code;
        this.jumpToCode(code, true);
      }
    });
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

  cardKind(item: ListItem): MediaCardKind {
    return VIDEO_TYPES.includes(item.media_type as any) ? 'video' : 'photo';
  }

  cardExtension(item: ListItem): string | null {
    const dot = item.file_name.lastIndexOf('.');
    return dot > 0 ? item.file_name.slice(dot + 1) : null;
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
        if (this.pendingCode === code) this.pendingCode = null;
        this.openItem(item, fromDeepLink);
      },
      error: () => {
        if (this.pendingCode === code) this.pendingCode = null;
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

  /** The detail panel changed some list membership (add/remove, possibly on a
      different list). Refresh this list's items + count; if the currently
      open item was removed from *this* list, close the panel too. */
  onListsChanged(): void {
    this.loadListMeta();
    const openMd5 = this.selectedItem()?.md5_hash;
    this.api.getListItems(this.listId, 1, PAGE_SIZE).subscribe({
      next: resp => {
        this.total.set(resp.total);
        this.page.set(resp.page);
        this.items.set(resp.items);
        if (openMd5 && !resp.items.some(i => i.md5_hash === openMd5)) {
          this.closeDetail();
        }
      },
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
