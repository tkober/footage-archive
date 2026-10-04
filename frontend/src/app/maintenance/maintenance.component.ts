import { Component } from '@angular/core';

import { MissingFilesComponent } from './missing-files/missing-files.component';
import { PathConflictsComponent } from './path-conflicts/path-conflicts.component';

@Component({
  selector: 'app-maintenance',
  standalone: true,
  imports: [PathConflictsComponent, MissingFilesComponent],
  templateUrl: './maintenance.component.html',
  styleUrl: './maintenance.component.css',
  host: { class: 'page-flush' },
})
export class MaintenanceComponent {}
