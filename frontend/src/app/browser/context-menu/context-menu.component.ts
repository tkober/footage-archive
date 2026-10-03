import { Component, EventEmitter, Input, Output } from '@angular/core';
import { PathChild } from '../../models';

export type ContextMenuActionKind = 'scan' | 'track' | 'rename' | 'move';

export interface ContextMenuActionEvent {
  kind: ContextMenuActionKind;
  entry: PathChild;
}

@Component({
  selector: 'app-context-menu',
  standalone: true,
  templateUrl: './context-menu.component.html',
  styleUrl: './context-menu.component.css',
})
export class ContextMenuComponent {
  @Input() entry!: PathChild;
  @Input() x = 0;
  @Input() y = 0;
  @Output() action = new EventEmitter<ContextMenuActionEvent>();
  @Output() close = new EventEmitter<void>();

  emit(kind: ContextMenuActionKind) {
    this.action.emit({ kind, entry: this.entry });
    this.close.emit();
  }
}
