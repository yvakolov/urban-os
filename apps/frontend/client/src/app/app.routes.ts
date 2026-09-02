import { Route } from '@angular/router';

/**
 * Маршруты приложения.
 *
 * Оболочка смонтирована в App и в дерево маршрутов не входит: шапка одна на
 * всё приложение и перемонтироваться при переходах не должна.
 */
export const appRoutes: Route[] = [
  {
    path: '',
    pathMatch: 'full',
    title: 'urban-os · обзор',
    loadComponent: () => import('./overview/overview-page').then((m) => m.OverviewPage),
  },
  {
    path: 'graph',
    title: 'Граф требований · urban-os',
    loadComponent: () =>
      import('./requirements-graph/requirements-graph-page').then(
        (m) => m.RequirementsGraphPage,
      ),
  },
  // Неизвестный путь ведёт на корень, а не показывает ошибку: витрина
  // публикуется на GitHub Pages, где прямой заход на любой путь отдаётся
  // через 404.html, и осмысленной страницы «не найдено» у неё нет.
  { path: '**', redirectTo: '' },
];
