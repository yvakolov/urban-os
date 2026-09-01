import { Route } from '@angular/router';

export const appRoutes: Route[] = [
  {
    path: '',
    loadComponent: () =>
      import('./requirements-graph/requirements-graph-page').then(
        (m) => m.RequirementsGraphPage,
      ),
  },
];
