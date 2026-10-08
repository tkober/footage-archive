import { Component, ElementRef, OnInit, computed, inject, input, output, signal } from '@angular/core';

import { NgTemplateOutlet } from '@angular/common';

import { ApiService } from '../../services/api.service';

/** One entry in the "add keyword" dropdown: an existing keyword to pick, or
    the trailing "add this keyword" affordance shown when there's no exact
    match. */
export type KeywordPickerEntry =
  | { type: 'existing'; keyword: string }
  | { type: 'create'; name: string };

/** Reusable "add keyword" input: a text field with a custom, keyboard-
    navigable suggestion dropdown of existing keywords plus a trailing
    "+ Add keyword …" entry. Unlike `app-list-picker` there's nothing to
    create here — picking either entry just emits the keyword (the existing
    spelling, for an exact match) and clears the input, ready for the next
    one; callers make whatever "add this keyword to the file(s)" API call
    is appropriate.

    `inline` renders the suggestions as an always-visible, scrollable list
    under the input instead of a floating dropdown (see list-picker's #68).
    `autofocus` focuses the input on open (mouse/trackpad only). */
@Component({
  selector: 'app-keyword-picker',
  standalone: true,
  imports: [NgTemplateOutlet],
  templateUrl: './keyword-picker.component.html',
  styleUrl: './keyword-picker.component.css',
})
export class KeywordPickerComponent implements OnInit {
  private api = inject(ApiService);
  private el = inject(ElementRef<HTMLElement>);

  // ── Inputs / Outputs ──
  exclude     = input<string[]>([]);
  placeholder = input('Find or add a keyword…');
  disabled    = input(false);
  inline      = input(false);
  autofocus   = input(false);
  picked      = output<string>();
  /** Esc pressed while the dropdown is already closed, or in inline mode —
      a first Esc with the floating dropdown open just closes it instead
      (see `onKeydown`), same two-step convention as `app-list-picker`. */
  escape      = output<void>();

  // ── Internal state ──
  value          = signal('');
  allKeywords    = signal<string[]>([]);
  dropdownOpen   = signal(false);
  highlightIndex = signal(0);

  entries = computed<KeywordPickerEntry[]>(() => {
    const input = this.value().trim();
    const inputLower = input.toLowerCase();
    const excluded = new Set(this.exclude().map(kw => kw.toLowerCase()));
    const candidates = this.allKeywords()
      .filter(kw => !excluded.has(kw.toLowerCase()))
      .filter(kw => inputLower === '' || kw.toLowerCase().includes(inputLower));
    const entries: KeywordPickerEntry[] = candidates.map(keyword => ({ type: 'existing', keyword }));
    const exactMatch = this.allKeywords().some(kw => kw.toLowerCase() === inputLower);
    if (input && !exactMatch) {
      entries.push({ type: 'create', name: input });
    }
    return entries;
  });

  ngOnInit() {
    if (this.inline()) this.loadKeywords();
    // After the surrounding popover has focused its own panel. Touch devices
    // are skipped: the on-screen keyboard would cover the list just opened.
    if (this.autofocus() && window.matchMedia('(pointer: fine)').matches) {
      setTimeout(() => this.el.nativeElement.querySelector('input')?.focus());
    }
  }

  onInputChange(value: string) {
    this.value.set(value);
    this.highlightIndex.set(0);
    this.dropdownOpen.set(true);
  }

  /** Refresh the keyword list each time focus is gained, so a keyword added
      elsewhere (or by another picker instance) shows up. */
  onFocus() {
    this.dropdownOpen.set(true);
    this.loadKeywords();
  }

  private loadKeywords() {
    this.api.getAllKeywords().subscribe(kws => this.allKeywords.set(kws));
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
    } else if (event.key === 'Escape') {
      // Inline (inside a popover): let Esc bubble so the popover closes.
      // Floating: a first Esc with the dropdown open just closes it; otherwise
      // it's handed to the caller via `escape` instead of bubbling on to close
      // whatever's hosting the picker (e.g. the whole detail panel).
      if (this.inline()) {
        this.escape.emit();
        return;
      }
      event.stopPropagation();
      if (this.dropdownOpen()) this.closeDropdown();
      else this.escape.emit();
    }
  }

  private scrollHighlightedIntoView() {
    setTimeout(() => this.el.nativeElement.querySelector('.keyword-picker-item.highlighted')
      ?.scrollIntoView({ block: 'nearest' }));
  }

  closeDropdown() {
    this.dropdownOpen.set(false);
  }

  pickEntry(entry: KeywordPickerEntry) {
    const keyword = entry.type === 'existing' ? entry.keyword : entry.name;
    if (!keyword) return;
    this.value.set('');
    this.closeDropdown();
    this.picked.emit(keyword);
  }
}
