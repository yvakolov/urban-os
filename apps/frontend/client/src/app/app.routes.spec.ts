import { appRoutes } from './app.routes';

describe('appRoutes', () => {
  it('отдаёт граф по собственному пути, а не с корня', () => {
    const graph = appRoutes.find((r) => r.path === 'graph');
    expect(graph).toBeDefined();
    expect(graph?.loadComponent).toBeDefined();
    expect(graph?.title).toContain('Граф требований');
  });

  it('на корне стоит обзор', () => {
    const root = appRoutes.find((r) => r.path === '');
    expect(root?.loadComponent).toBeDefined();
    // Без pathMatch: 'full' пустой префикс совпал бы с любым адресом,
    // и до маршрута графа управление не дошло бы никогда.
    expect(root?.pathMatch).toBe('full');
  });

  it('неизвестный путь не роняет приложение', () => {
    expect(appRoutes.at(-1)?.path).toBe('**');
  });
});
