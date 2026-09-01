import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { catchError, of } from 'rxjs';
import { GraphDataService } from './graph-data.service';
import { GraphRenderer } from './graph-renderer';
import type { GraphNode, RequirementsGraphFile } from './graph.types';

@Component({
  selector: 'app-requirements-graph-page',
  templateUrl: './requirements-graph-page.html',
  styleUrl: './requirements-graph-page.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RequirementsGraphPage {
  private readonly dataService = inject(GraphDataService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly hostRef = viewChild<ElementRef<HTMLDivElement>>('host');
  protected readonly svgRef = viewChild<ElementRef<SVGSVGElement>>('svg');

  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly searchTerm = signal('');
  protected readonly collapseRules = signal(true);
  protected readonly selectedNode = signal<GraphNode | null>(null);
  protected readonly data = signal<RequirementsGraphFile | null>(null);

  private renderer: GraphRenderer | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    this.dataService
      .load()
      .pipe(
        catchError((err: unknown) => {
          console.error('Failed to load requirements graph', err);
          this.error.set('Не удалось загрузить граф требований.');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed(),
      )
      .subscribe((file) => {
        if (!file) return;
        this.loading.set(false);
        this.data.set(file);
      });

    effect(() => {
      const svg = this.svgRef();
      const host = this.hostRef();
      const file = this.data();
      if (!svg || !host || !file || this.renderer) return;

      const renderer = new GraphRenderer(svg.nativeElement, {
        onSelect: (node) => this.selectedNode.set(node),
      });
      renderer.setData(file);
      renderer.setCollapseRules(this.collapseRules());
      this.renderer = renderer;

      const el = host.nativeElement;
      renderer.resize(el.clientWidth, el.clientHeight);
      this.resizeObserver = new ResizeObserver(([entry]) => {
        const { width, height } = entry.contentRect;
        renderer.resize(width, height);
      });
      this.resizeObserver.observe(el);
    });

    this.destroyRef.onDestroy(() => {
      this.renderer?.destroy();
      this.resizeObserver?.disconnect();
    });
  }

  protected onSearchInput(value: string): void {
    this.searchTerm.set(value);
    this.renderer?.setSearchTerm(value);
  }

  protected onToggleCollapse(collapsed: boolean): void {
    this.collapseRules.set(collapsed);
    this.renderer?.setCollapseRules(collapsed);
  }

  protected onReset(): void {
    this.searchTerm.set('');
    this.renderer?.reset();
  }
}
