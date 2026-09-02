import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { OverviewPage } from './overview-page';
import type { RequirementsGraphFile } from '../requirements-graph/graph.types';

const FILE = {
  generatedFrom: 'test',
  note: '',
  counts: { source: 2, branch: 0, profile: 2, rule: 3, deliverable: 1 },
  nodes: [
    { id: 'src:a', layer: 'source', label: 'A', status: 'active', transcribed: 'full', monitored: true, url: '' },
    { id: 'src:b', layer: 'source', label: 'B', status: 'active', transcribed: 'partial', monitored: false, url: '' },
    { id: 'profile:p1', layer: 'profile', label: 'p1', status: 'active', ruleCount: 2 },
    { id: 'profile:p2', layer: 'profile', label: 'p2', status: 'draft', ruleCount: 1 },
    { id: 'rule:r1', layer: 'rule', label: 'r1', ruleId: 'r1', profileId: 'p1', category: 'c', severity: 'blocker', verification: 'automatic', pending: false, sourceRef: '' },
    { id: 'rule:r2', layer: 'rule', label: 'r2', ruleId: 'r2', profileId: 'p1', category: 'c', severity: 'minor', verification: 'expert_evidence', pending: false, sourceRef: '' },
    { id: 'rule:r3', layer: 'rule', label: 'r3', ruleId: 'r3', profileId: 'p2', category: 'c', severity: 'major', verification: 'external_fact', pending: false, sourceRef: '' },
  ],
  links: [],
} as unknown as RequirementsGraphFile;

describe('OverviewPage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [OverviewPage],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  function render() {
    const fixture = TestBed.createComponent(OverviewPage);
    fixture.detectChanges();
    http.expectOne('assets/requirements-graph.json').flush(FILE);
    fixture.detectChanges();
    return fixture;
  }

  it('считает автоматизацию по правилам, а не по профилям', () => {
    const c = render().componentInstance as unknown as {
      automatic(): number;
      automationPercent(): number;
    };
    expect(c.automatic()).toBe(1);
    expect(c.automationPercent()).toBe(33);
  });

  it('раскладывает правила по профилям, а не делит поровну', () => {
    const c = render().componentInstance as unknown as {
      profiles(): { id: string; rules: number; automatic: number; blockers: number }[];
    };
    const rows = c.profiles();
    expect(rows.map((r) => r.id)).toEqual(['p1', 'p2']);
    expect(rows[0]).toMatchObject({ rules: 2, automatic: 1, blockers: 1 });
    expect(rows[1]).toMatchObject({ rules: 1, automatic: 0, blockers: 0 });
  });

  it('не падает и говорит об ошибке, если данные не загрузились', () => {
    const fixture = TestBed.createComponent(OverviewPage);
    fixture.detectChanges();
    http.expectOne('assets/requirements-graph.json').error(new ProgressEvent('fail'));
    fixture.detectChanges();
    const c = fixture.componentInstance as unknown as {
      error(): string | null;
      loading(): boolean;
    };
    expect(c.loading()).toBe(false);
    expect(c.error()).toBeTruthy();
  });
});
