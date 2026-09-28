import { Component, computed, inject, input, output, signal } from '@angular/core';

import { ApiService } from '../../services/api.service';
import { FileList } from '../../models';

/** One entry in the "add to list" dropdown: an existing list to join, or the
    trailing "create a new list" affordance shown when there's no exact match. */
export type ListPickerEntry =
  | { type: 'existing'; list: FileList }
  | { type: 'create'; name: string };

/** Reusable "add to list" input: a text field with a custom, keyboard-navigable
    suggestion dropdown of existing lists plus a trailing "+ Create list …"
    entry. Selecting an existing list just emits it; selecting the create
    entry creates the list itself (handling a 409 duplicate-name race inline)
    and then emits the newly created list. Callers are responsible for
    whatever "add to X" call the picked list should trigger. */
@Component({
  selector: 'app-list-picker',
  standalone: true,
  templateUrl: './list-picker.component.html',
  styleUrl: './list-picker.component.css',
})
export class ListPickerComponent {
  private api = inject(ApiService);

  // ── Inputs / Outputs ──
  excludeListIds = input<number[]>([]);
  placeholder    = input('Add to list…');
  disabled       = input(false);
  picked         = output<FileList>();

  // ── Internal state ──
  value          = signal('');
  allLists       = signal<FileList[]>([]);
  dropdownOpen   = signal(false);
  highlightIndex = signal(0);
  error          = signal<string | null>(null);

  entries = computed<ListPickerEntry[]>(() => {
    const input = this.value().trim();
    const inputLower = input.toLowerCase();
    const excluded = new Set(this.excludeListIds());
    const candidates = this.allLists()
      .filter(l => !excluded.has(l.id))
      .filter(l => inputLower === '' || l.name.toLowerCase().includes(inputLower));
    const entries: ListPickerEntry[] = candidates.map(list => ({ type: 'existing', list }));
    const exactMatch = this.allLists().some(l => l.name.toLowerCase() === inputLower);
    if (input && !exactMatch) {
      entries.push({ type: 'create', name: input });
    }
    return entries;
  });

  onInputChange(value: string) {
    this.value.set(value);
    this.error.set(null);
    this.highlightIndex.set(0);
    this.dropdownOpen.set(true);
  }

  /** Refresh the list of lists each time focus is gained, so lists created
      elsewhere (or by another picker instance) show up. */
  onFocus() {
    this.dropdownOpen.set(true);
    this.api.getLists().subscribe(ls => this.allLists.set(ls));
  }

  /** Delay long enough for a dropdown-item mousedown to be handled first
      (mousedown, not click, so it fires before blur). */
  onBlur() {
    setTimeout(() => this.dropdownOpen.set(false), 150);
  }

  onKeydown(event: KeyboardEvent) {
    const entries = this.entries();
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      this.dropdownOpen.set(true);
      this.highlightIndex.update(i => Math.min(i + 1, Math.max(entries.length - 1, 0)));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      this.highlightIndex.update(i => Math.max(i - 1, 0));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const entry = entries[this.highlightIndex()];
      if (entry) this.pickEntry(entry);
    } else if (event.key === 'Escape') {
      this.closeDropdown();
      event.stopPropagation();
    }
  }

  closeDropdown() {
    this.dropdownOpen.set(false);
  }

  pickEntry(entry: ListPickerEntry) {
    if (entry.type === 'existing') this.pickExisting(entry.list);
    else this.createAndPick(entry.name);
  }

  private pickExisting(list: FileList) {
    this.error.set(null);
    this.value.set('');
    this.closeDropdown();
    this.picked.emit(list);
  }

  private createAndPick(name: string) {
    const trimmed = name.trim();
    if (!trimmed) return;
    this.error.set(null);
    this.api.createList(trimmed).subscribe({
      next: list => {
        this.allLists.update(ls => [...ls, list]);
        this.pickExisting(list);
      },
      error: err => this.error.set(err.error?.detail ?? 'Failed to create list'),
    });
  }
}
