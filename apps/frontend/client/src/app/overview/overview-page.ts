import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { catchError, of } from 'rxjs';
import { HlmCardImports } from '@spartan-ng/helm/card';
import { HlmBadgeImports } from '@spartan-ng/helm/badge';
import { HlmTableImports } from '@spartan-ng/helm/table';
import { HlmButtonImports } from '@spartan-ng/helm/button';
import { GraphDataService } from '../requirements-graph/graph-data.service';
import type { RequirementsGraphFile, RuleNode, SourceNode } from '../requirements-graph/graph.types';

interface ProfileRow {
  id: string;
  status: string;
  rules: number;
  automatic: number;
  automationPercent: number;
  blockers: number;
}

/**
 * Обзор состояния корпуса требований.
 *
 * Считается из того же файла графа, что рисует граф: отдельного источника
 * цифр нет намеренно — иначе обзор и граф начали бы расходиться, и было бы
 * непонятно, какой из них врёт.
 */
@Component({
  selector: 'app-overview-page',
  imports: [RouterLink, HlmCardImports, HlmBadgeImports, HlmTableImports, HlmButtonImports],
  templateUrl: './overview-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OverviewPage {
  private readonly dataService = inject(GraphDataService);

  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly data = signal<RequirementsGraphFile | null>(null);

  constructor() {
    this.dataService
      .load()
      .pipe(
        catchError((err: unknown) => {
          console.error('Failed to load requirements graph', err);
          this.error.set('Не удалось загрузить данные требований.');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed(),
      )
      .subscribe((file) => {
        if (!file) return;
        this.data.set(file);
        this.loading.set(false);
      });
  }

  private readonly rules = computed<RuleNode[]>(
    () => (this.data()?.nodes.filter((n) => n.layer === 'rule') as RuleNode[]) ?? [],
  );

  private readonly sources = computed<SourceNode[]>(
    () => (this.data()?.nodes.filter((n) => n.layer === 'source') as SourceNode[]) ?? [],
  );

  protected readonly counts = computed(() => this.data()?.counts ?? null);

  protected readonly automatic = computed(
    () => this.rules().filter((r) => r.verification === 'automatic').length,
  );

  protected readonly automationPercent = computed(() => {
    const total = this.rules().length;
    return total ? Math.round((this.automatic() / total) * 100) : 0;
  });

  protected readonly monitored = computed(() => this.sources().filter((s) => s.monitored).length);

  protected readonly transcribedFull = computed(
    () => this.sources().filter((s) => s.transcribed === 'full').length,
  );

  protected readonly bySeverity = computed(() => {
    const rules = this.rules();
    return (['blocker', 'major', 'minor'] as const).map((severity) => ({
      severity,
      count: rules.filter((r) => r.severity === severity).length,
    }));
  });

  protected readonly byVerification = computed(() => {
    const rules = this.rules();
    return (
      ['automatic', 'expert_evidence', 'external_fact', 'external_verdict'] as const
    ).map((verification) => ({
      verification,
      count: rules.filter((r) => r.verification === verification).length,
    }));
  });

  protected readonly profiles = computed<ProfileRow[]>(() => {
    const file = this.data();
    if (!file) return [];
    const rules = this.rules();
    return file.nodes
      .filter((n) => n.layer === 'profile')
      .map((p) => {
        const own = rules.filter((r) => r.profileId === p.label);
        const auto = own.filter((r) => r.verification === 'automatic').length;
        return {
          id: p.label,
          status: p.status,
          rules: own.length,
          automatic: auto,
          automationPercent: own.length ? Math.round((auto / own.length) * 100) : 0,
          blockers: own.filter((r) => r.severity === 'blocker').length,
        };
      })
      .sort((a, b) => b.rules - a.rules);
  });

  /** Цвет значка соответствует смыслу, а не порядку в списке. */
  protected severityVariant(severity: string): 'destructive' | 'secondary' | 'outline' {
    if (severity === 'blocker') return 'destructive';
    if (severity === 'major') return 'secondary';
    return 'outline';
  }

  protected verificationLabel(verification: string): string {
    return (
      {
        automatic: 'Машинная проверка',
        expert_evidence: 'Заключение эксперта',
        external_fact: 'Внешний факт',
        external_verdict: 'Заключение ведомства',
      }[verification] ?? verification
    );
  }
}
