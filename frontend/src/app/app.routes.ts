import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', redirectTo: 'browser', pathMatch: 'full' },
  {
    path: 'browser',
    title: 'Browser',
    loadComponent: () => import('./browser/browser.component').then(m => m.BrowserComponent)
  },
  {
    path: 'map',
    title: 'Map',
    loadComponent: () => import('./map/map.component').then(m => m.MapComponent)
  },
  {
    path: 'settings',
    title: 'Settings',
    loadComponent: () => import('./settings/settings.component').then(m => m.SettingsComponent)
  },
];
