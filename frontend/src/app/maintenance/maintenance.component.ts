import { Component } from '@angular/core';

import { MissingFilesComponent } from './missing-files/missing-files.component';

@Component({
  selector: 'app-maintenance',
  standalone: true,
  imports: [MissingFilesComponent],
  templateUrl: './maintenance.component.html',
  styleUrl: './maintenance.component.css',
})
export class MaintenanceComponent {}
