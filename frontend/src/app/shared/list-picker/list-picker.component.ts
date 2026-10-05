import { Component, ElementRef, OnInit, computed, inject, input, output, signal } from '@angular/core';

import { NgTemplateOutlet } from '@angular/common';

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
    whatever "add to X" call the picked list should trigger.

    `inline` renders the suggestions as an always-visible, scrollable list
    under the input instead of a floating dropdown: inside a popover the
    dropdown got clipped by the panel's own scroll box, leaving only a sliver
    of it visible (#68). `autofocus` focuses the input on open (mouse/trackpad only). */
@Component({
  selector: 'app-list-picker',
  standalone: true,
  imports: [NgTemplateOutlet],
  templateUrl: './list-picker.component.html',
  styleUrl: './list-picker.component.css',
})
export class ListPickerComponent implements OnInit {
  private api = inject(ApiService);
  private el = inject(ElementRef<HTMLElement>);

  // ── Inputs / Outputs ──
  excludeListIds = input<number[]>([]);
  placeholder    = input('Add to list…');
  disabled       = input(false);
  inline         = input(false);
  autofocus      = input(false);
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

  ngOnInit() {
    if (this.inline()) this.loadLists();
    // After the surrounding popover has focused its own panel. Touch devices
    // are skipped: the on-screen keyboard would cover the list just opened.
    if (this.autofocus() && window.matchMedia('(pointer: fine)').matches) {
      setTimeout(() => this.el.nativeElement.querySelector('input')?.focus());
    }
  }

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
    this.loadLists();
  }

  private loadLists() {
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
      this.scrollHighlightedIntoView();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      this.highlightIndex.update(i => Math.max(i - 1, 0));
      this.scrollHighlightedIntoView();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const entry = entries[this.highlightIndex()];
      if (entry) this.pickEntry(entry);
    } else if (event.key === 'Escape' && this.dropdownOpen() && !this.inline()) {
      // Only swallow Esc while the suggestions are open; a second Esc then
      // reaches the surrounding popover/dialog and closes it.
      this.closeDropdown();
      event.stopPropagation();
    }
  }

  private scrollHighlightedIntoView() {
    setTimeout(() => this.el.nativeElement.querySelector('.list-picker-item.highlighted')
      ?.scrollIntoView({ block: 'nearest' }));
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
