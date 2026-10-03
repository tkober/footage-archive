import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', redirectTo: 'browser', pathMatch: 'full' },
  {
    path: 'browser',
    title: 'Browser',
    loadComponent: () => import('./browser/browser.component').then(m => m.BrowserComponent)
  },
  {
    path: 'search',
    title: 'Search',
    loadComponent: () => import('./search/search.component').then(m => m.SearchComponent)
  },
  {
    path: 'lists',
    title: 'Lists',
    loadComponent: () => import('./lists/lists.component').then(m => m.ListsComponent)
  },
  {
    path: 'lists/:id',
    title: 'Lists',
    loadComponent: () => import('./lists/list-detail.component').then(m => m.ListDetailComponent)
  },
  {
    path: 'map',
    title: 'Map',
    loadComponent: () => import('./map/map.component').then(m => m.MapComponent)
  },
  {
    path: 'maintenance',
    title: 'Maintenance',
    loadComponent: () => import('./maintenance/maintenance.component').then(m => m.MaintenanceComponent)
  },
  {
    path: 'settings',
    title: 'Settings',
    loadComponent: () => import('./settings/settings.component').then(m => m.SettingsComponent)
  },
];
