import { computeReachable, projectToVisible } from './graph-highlight';
import type { GraphLinkRaw, GraphNode } from './graph.types';

// Minimal fixture mirroring the real shape: source -> branch -> rule -> deliverable,
// plus one rule the branch does NOT impact, to prove dimming/scoping works.
// Note rule.profileId ('p') is deliberately a bare slug, not the profile node id
// ('profile:p') — that's how the real data file is shaped, and projectToVisible
// must resolve the mismatch via profileNodeId().
const nodes: GraphNode[] = [
  { id: 'src:a', layer: 'source', label: 'A', status: 'active', transcribed: 'full', monitored: true, url: '' },
  { id: 'branch:a:x', layer: 'branch', label: 'x', sourceId: 'a', ruleCount: 1, registry: false },
  { id: 'profile:p', layer: 'profile', label: 'p', status: 'active', ruleCount: 2 },
  {
    id: 'rule:r1', layer: 'rule', label: 'r1', ruleId: 'r1', profileId: 'p',
    category: 'c', severity: 'blocker', verification: 'automatic', pending: false, sourceRef: '',
  },
  {
    id: 'rule:r2', layer: 'rule', label: 'r2', ruleId: 'r2', profileId: 'profile:p',
    category: 'c', severity: 'minor', verification: 'automatic', pending: false, sourceRef: '',
  },
  { id: 'deliv:d1', layer: 'deliverable', label: 'd1', kind: 'file', origin: '', fmt: '', blockedBy: null, isOutcome: false },
];

const links: GraphLinkRaw[] = [
  { source: 'src:a', target: 'branch:a:x', kind: 'branch' },
  { source: 'src:a', target: 'profile:p', kind: 'transcribedInto' },
  { source: 'profile:p', target: 'rule:r1', kind: 'contains' },
  { source: 'profile:p', target: 'rule:r2', kind: 'contains' },
  { source: 'branch:a:x', target: 'rule:r1', kind: 'impacts' },
  { source: 'rule:r1', target: 'deliv:d1', kind: 'produces' },
];

describe('computeReachable', () => {
  it('walks downstream from a branch to only the rule it impacts and its deliverable', () => {
    const result = computeReachable(nodes, links, 'branch:a:x', 'down');
    expect(result.nodeIds).toEqual(new Set(['branch:a:x', 'rule:r1', 'deliv:d1']));
    expect(result.nodeIds.has('rule:r2')).toBe(false);
  });

  it('walks upstream from a deliverable back to the rule, its profile, the branch that impacts it, and the source', () => {
    const result = computeReachable(nodes, links, 'deliv:d1', 'up');
    // rule:r1 has two upstream parents (contains from profile:p, impacts from branch:a:x) —
    // both are legitimately part of "which requirements justify this artifact, and where do they come from".
    expect(result.nodeIds).toEqual(
      new Set(['deliv:d1', 'rule:r1', 'profile:p', 'branch:a:x', 'src:a']),
    );
  });
});

describe('projectToVisible', () => {
  it('maps a hidden rule onto its visible profile bubble', () => {
    const nodesById = new Map(nodes.map((n) => [n.id, n]));
    const visibleIds = new Set(['branch:a:x', 'profile:p', 'deliv:d1']); // rules collapsed
    const projected = projectToVisible(new Set(['branch:a:x', 'rule:r1', 'deliv:d1']), nodesById, visibleIds);
    expect(projected).toEqual(new Set(['branch:a:x', 'profile:p', 'deliv:d1']));
  });
});
