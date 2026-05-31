import { Component, EventEmitter, Input, Output } from '@angular/core';
import { PathChild } from '../../models';

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
  @Output() action = new EventEmitter<PathChild>();
  @Output() close = new EventEmitter<void>();
}
