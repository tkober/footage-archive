import { Component, ElementRef, OnInit, ViewChild, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';

import { ApiService } from '../services/api.service';
import { IconComponent } from '../shared/icon/icon.component';
import { MenuComponent, MenuItem } from '../shared/menu/menu.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { FileList } from '../models';

@Component({
  selector: 'app-lists',
  standalone: true,
  imports: [FormsModule, ConfirmDialogComponent, IconComponent, MenuComponent],
  host: { class: 'page-flush' },
  templateUrl: './lists.component.html',
  styleUrl: './lists.component.css',
})
export class ListsComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  lists = signal<FileList[]>([]);
  /** Up to four preview URLs per list for the tile mosaic (#45). */
  previews = signal<Partial<Record<number, string[]>>>({});
  readonly listMenuItems: MenuItem[] = [
    { id: 'open', label: 'Open', icon: 'eye' },
    { id: 'rename', label: 'Rename', icon: 'edit' },
    { id: 'delete', label: 'Delete list…', icon: 'trash', danger: true, separatorBefore: true },
  ];
  menuFor = signal<{ list: FileList; anchor: HTMLElement } | null>(null);
  loading = signal(false);

  // ── Create ──
  newListName = signal('');
  createError = signal<string | null>(null);

  // ── Rename ──
  editingId = signal<number | null>(null);
  editNameValue = signal('');
  renameError = signal<string | null>(null);
  @ViewChild('renameInput') renameInputRef?: ElementRef<HTMLInputElement>;

  // ── Delete ──
  pendingDelete = signal<FileList | null>(null);

  ngOnInit(): void {
    this.loadLists();
  }

  private loadLists(): void {
    this.loading.set(true);
    this.api.getLists().subscribe({
      next: lists => { this.lists.set(lists); this.loading.set(false); lists.forEach(l => this.loadPreviews(l)); },
      error: () => this.loading.set(false),
    });
  }

  private loadPreviews(list: FileList): void {
    if (!list.item_count) return;
    this.api.getListItems(list.id, 1, 4).subscribe({
      next: resp => this.previews.update(p => ({
        ...p, [list.id]: resp.items.map(i => this.api.clipPreviewUrl(i.md5_hash)),
      })),
    });
  }

  openMenu(list: FileList, anchor: HTMLElement, event: Event): void {
    event.stopPropagation();
    this.menuFor.set({ list, anchor });
  }

  onMenuSelect(id: string): void {
    const target = this.menuFor();
    this.menuFor.set(null);
    if (!target) return;
    const ev = new Event('menu');
    if (id === 'open') this.openList(target.list);
    if (id === 'rename') this.startRename(target.list, ev);
    if (id === 'delete') this.requestDelete(target.list, ev);
  }

  createList(): void {
    const name = this.newListName().trim();
    if (!name) return;
    this.createError.set(null);
    this.api.createList(name).subscribe({
      next: list => {
        this.lists.update(ls => [...ls, list]);
        this.newListName.set('');
      },
      error: err => {
        this.createError.set(
          err.status === 409
            ? 'A list with this name already exists'
            : (err.error?.detail ?? 'Failed to create list')
        );
      },
    });
  }

  openList(list: FileList): void {
    this.router.navigate(['/lists', list.id]);
  }

  startRename(list: FileList, event: Event): void {
    event.stopPropagation();
    this.editingId.set(list.id);
    this.editNameValue.set(list.name);
    this.renameError.set(null);
    setTimeout(() => this.renameInputRef?.nativeElement.focus(), 0);
  }

  saveRename(list: FileList): void {
    const name = this.editNameValue().trim();
    if (!name || name === list.name) { this.cancelRename(); return; }
    this.api.renameList(list.id, name).subscribe({
      next: updated => {
        this.lists.update(ls => ls.map(l => l.id === updated.id ? updated : l));
        this.editingId.set(null);
      },
      error: err => {
        this.renameError.set(
          err.status === 409
            ? 'A list with this name already exists'
            : (err.error?.detail ?? 'Rename failed')
        );
      },
    });
  }

  cancelRename(event?: Event): void {
    event?.stopPropagation();
    this.editingId.set(null);
    this.renameError.set(null);
  }

  requestDelete(list: FileList, event: Event): void {
    event.stopPropagation();
    this.pendingDelete.set(list);
  }

  cancelDelete(): void {
    this.pendingDelete.set(null);
  }

  confirmDelete(): void {
    const list = this.pendingDelete();
    if (!list) return;
    this.api.deleteList(list.id).subscribe({
      next: () => {
        this.lists.update(ls => ls.filter(l => l.id !== list.id));
        this.pendingDelete.set(null);
      },
      error: () => this.pendingDelete.set(null),
    });
  }
}
