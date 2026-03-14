import { Component } from '@angular/core';

@Component({
  selector: 'app-browser',
  standalone: true,
  template: `<p class="placeholder">Browser coming soon</p>`,
  styles: [`
    .placeholder {
      color: #71717a;
      font-size: 0.9rem;
    }
  `]
})
export class BrowserComponent {}
