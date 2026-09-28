import { Component, ElementRef, OnInit, ViewChild, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';

import { ApiService } from '../services/api.service';
import { ConfirmDialogComponent } from '../shared/confirm-dialog/confirm-dialog.component';
import { FileList } from '../models';

@Component({
  selector: 'app-lists',
  standalone: true,
  imports: [FormsModule, ConfirmDialogComponent],
  templateUrl: './lists.component.html',
  styleUrl: './lists.component.css',
})
export class ListsComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  lists = signal<FileList[]>([]);
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
      next: lists => { this.lists.set(lists); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
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
