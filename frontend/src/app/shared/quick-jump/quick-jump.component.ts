import { Component, ElementRef, HostListener, OnInit, ViewChild, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NavigationEnd, Router } from '@angular/router';
import { filter } from 'rxjs';

import { ApiService } from '../../services/api.service';
import { FileList } from '../../models';
import { IconComponent } from '../icon/icon.component';
import { MenuComponent, MenuItem } from '../menu/menu.component';

const STORAGE_KEY = 'quickJump.listId';

/** Compact "list + code -> photo" jump box for the header. The gachapon use
    case: a capsule (with a printed code) in hand, pick its list, type the
    code, hit Enter, land straight on that item in the list view. Global
    Cmd/Ctrl+K focuses the code field; on narrow screens the field collapses
    to an icon button that expands it as an overlay. */
@Component({
  selector: 'app-quick-jump',
  standalone: true,
  imports: [FormsModule, IconComponent, MenuComponent],
  templateUrl: './quick-jump.component.html',
  styleUrl: './quick-jump.component.css',
})
export class QuickJumpComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  @ViewChild('codeInput') private codeInputRef?: ElementRef<HTMLInputElement>;
  @ViewChild('listBtn') private listBtnRef?: ElementRef<HTMLButtonElement>;

  lists = signal<FileList[]>([]);
  selectedListId = signal<number | null>(null);
  code = signal('');
  error = signal(false);
  listMenuOpen = signal(false);
  mobileExpanded = signal(false);

  readonly isMac = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform);

  ngOnInit(): void {
    this.loadLists();
    // Lists may be created/deleted anywhere in the app; refresh on navigation
    // so the box appears once the first list exists.
    this.router.events
      .pipe(filter(e => e instanceof NavigationEnd))
      .subscribe(() => this.loadLists());
  }

  @HostListener('document:keydown', ['$event'])
  onGlobalKeydown(event: KeyboardEvent): void {
    if (event.key.toLowerCase() !== 'k' || !(event.metaKey || event.ctrlKey)) return;
    if (!this.lists().length) return;
    event.preventDefault();
    this.mobileExpanded.set(true);
    queueMicrotask(() => this.codeInputRef?.nativeElement.focus());
  }

  /** Refresh the list options. Keeps the current selection while it still
      exists; otherwise falls back to the remembered list, then the first. */
  private loadLists(): void {
    this.api.getLists().subscribe(lists => {
      this.lists.set(lists);
      if (lists.some(l => l.id === this.selectedListId())) return;
      const stored = Number(localStorage.getItem(STORAGE_KEY));
      const remembered = lists.find(l => l.id === stored);
      this.selectedListId.set(remembered ? remembered.id : (lists[0]?.id ?? null));
    });
  }

  selectedListName(): string {
    return this.lists().find(l => l.id === this.selectedListId())?.name ?? '';
  }

  listMenuItems(): MenuItem[] {
    return this.lists().map(l => ({ id: String(l.id), label: l.name }));
  }

  listMenuAnchor(): HTMLElement | null {
    return this.listBtnRef?.nativeElement ?? null;
  }

  openListMenu(): void {
    this.loadLists();
    this.listMenuOpen.set(true);
  }

  closeListMenu(): void {
    this.listMenuOpen.set(false);
  }

  onListMenuSelect(id: string): void {
    this.onListChange(Number(id));
  }

  onListChange(id: number): void {
    this.selectedListId.set(id);
    localStorage.setItem(STORAGE_KEY, String(id));
  }

  onCodeInput(value: string): void {
    this.code.set(value.toUpperCase());
    this.error.set(false);
  }

  expand(): void {
    this.mobileExpanded.set(true);
    queueMicrotask(() => this.codeInputRef?.nativeElement.focus());
  }

  collapse(): void {
    this.mobileExpanded.set(false);
  }

  jump(): void {
    const listId = this.selectedListId();
    const code = this.code().trim();
    if (!listId || !code) return;
    this.error.set(false);
    this.api.getListItemByCode(listId, code).subscribe({
      next: () => {
        this.code.set('');
        this.mobileExpanded.set(false);
        this.router.navigate(['/lists', listId], { queryParams: { code } });
      },
      error: () => this.error.set(true),
    });
  }
}
