import { Component, inject } from '@angular/core';

import { ThemeChoice, ThemeService } from '../services/theme.service';

@Component({
  selector: 'app-settings',
  standalone: true,
  templateUrl: './settings.component.html',
  styleUrl: './settings.component.css'
})
export class SettingsComponent {
  private themeService = inject(ThemeService);

  choice = this.themeService.choice;

  readonly options: { value: ThemeChoice; label: string }[] = [
    { value: 'system', label: 'System' },
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
  ];

  select(choice: ThemeChoice) {
    this.themeService.setChoice(choice);
  }
}
