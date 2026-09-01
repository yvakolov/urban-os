import {
  type Selection,
  drag,
  forceCollide,
  forceLink,
  forceSimulation,
  forceX,
  forceY,
  linkHorizontal,
  scalePoint,
  select,
  zoom,
  zoomIdentity,
} from 'd3';
import { computeReachable, directionForLayer, projectToVisible } from './graph-highlight';
import { profileNodeId } from './graph.types';
import type {
  DeliverableKind,
  GraphLayer,
  GraphLinkRaw,
  GraphNode,
  RequirementsGraphFile,
  SimLink,
  SimNode,
} from './graph.types';

const LAYER_ORDER: GraphLayer[] = ['source', 'branch', 'profile', 'rule', 'deliverable'];
const KIND_ORDER: DeliverableKind[] = ['form', 'file', 'package', 'external', 'verdict'];

const linkPath = linkHorizontal<SimLink, SimNode>()
  .source((l) => l.source as SimNode)
  .target((l) => l.target as SimNode)
  .x((d) => d.x ?? 0)
  .y((d) => d.y ?? 0);

function isRuleNode(node: GraphNode): node is Extract<GraphNode, { layer: 'rule' }> {
  return node.layer === 'rule';
}

function truncateLabel(label: string, max = 28): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

function sourceShortId(id: string): string {
  return id.startsWith('src:') ? id.slice(4) : id;
}

/** How hard each layer is pulled back to its deterministic band — rule/deliverable need more
 *  pull to hold shape against their many links; profile least of all since there are only 5. */
function yStrength(layer: GraphLayer): number {
  switch (layer) {
    case 'profile':
      return 0.5;
    case 'rule':
    case 'deliverable':
      return 0.3;
    default:
      return 0.25;
  }
}

/**
 * Splits [0,1] into consecutive slices sized proportionally to each group's member count
 * (so a group of 49 gets ~7x the vertical room of a group of 7, never an equal fixed share),
 * then spreads that group's own items evenly within its slice. Used to give every node in a
 * column — sources, branches, profiles, deliverables-by-kind, rules-by-profile — a unique,
 * deterministic target position that by construction cannot collide with its neighbours,
 * instead of leaving collision force to fight overlap out of a shared attraction point.
 */
function stackedFractions<T extends { id: string }>(
  items: T[],
  groupKeyOf: (item: T) => string,
  groupOrder: string[],
  gap = 0,
): Map<string, number> {
  const byGroup = new Map<string, T[]>();
  for (const item of items) {
    const key = groupKeyOf(item);
    const bucket = byGroup.get(key);
    if (bucket) bucket.push(item);
    else byGroup.set(key, [item]);
  }
  const total = items.length || 1;
  // Reserve a seam between adjacent groups (not around the outer edges) so e.g. rules from
  // different profiles read as distinct clusters instead of one seamless gradient — proportional
  // sizing alone guarantees no *overlap*, but says nothing about them being visually separated.
  const usableSpan = Math.max(1 - gap * Math.max(groupOrder.length - 1, 0), 0);
  const fractions = new Map<string, number>();
  let cursor = 0;
  for (const key of groupOrder) {
    const group = byGroup.get(key) ?? [];
    const span = (group.length / total) * usableSpan;
    group.forEach((item, i) => fractions.set(item.id, cursor + ((i + 0.5) / group.length) * span));
    cursor += span + gap;
  }
  return fractions;
}

function matchesSearch(node: GraphNode, term: string): boolean {
  if (node.label.toLowerCase().includes(term)) return true;
  if (isRuleNode(node)) {
    return node.ruleId.toLowerCase().includes(term) || node.category.toLowerCase().includes(term);
  }
  return false;
}

export interface GraphRendererCallbacks {
  onSelect: (node: GraphNode | null) => void;
}

/** Owns the D3 force simulation and all SVG drawing; the Angular component only feeds it state. */
export class GraphRenderer {
  private readonly svg: Selection<SVGSVGElement, unknown, null, undefined>;
  private readonly zoomLayer: Selection<SVGGElement, unknown, null, undefined>;
  private readonly bandLayer: Selection<SVGGElement, unknown, null, undefined>;
  private readonly linkLayer: Selection<SVGGElement, unknown, null, undefined>;
  private readonly highlightLayer: Selection<SVGGElement, unknown, null, undefined>;
  private readonly nodeLayer: Selection<SVGGElement, unknown, null, undefined>;

  private width = 800;
  private height = 600;

  private fullNodes: GraphNode[] = [];
  private fullLinks: GraphLinkRaw[] = [];
  private nodesById = new Map<string, GraphNode>();
  private profileOrder: string[] = [];
  private profileCount = 1;
  /** Each node's target y as a [0,1] fraction of the current height — see {@link stackedFractions}. */
  private yFraction = new Map<string, number>();

  private simNodes: SimNode[] = [];
  private simLinks: SimLink[] = [];
  private positionById = new Map<string, SimNode>();

  private collapseRules = true;
  private searchTerm = '';
  private hoveredId: string | null = null;
  private pinnedId: string | null = null;

  private readonly simulation = forceSimulation<SimNode>();
  private readonly xScale = scalePoint<GraphLayer>().domain(LAYER_ORDER).padding(0.5);

  constructor(
    svgEl: SVGSVGElement,
    private readonly callbacks: GraphRendererCallbacks,
  ) {
    this.svg = select(svgEl);
    this.zoomLayer = this.svg.append('g').attr('class', 'zoom-layer');
    this.bandLayer = this.zoomLayer.append('g').attr('class', 'band-layer');
    this.linkLayer = this.zoomLayer.append('g').attr('class', 'link-layer');
    this.highlightLayer = this.zoomLayer.append('g').attr('class', 'highlight-layer');
    this.nodeLayer = this.zoomLayer.append('g').attr('class', 'node-layer');

    this.svg.on('click', (event: MouseEvent) => {
      if (event.target === svgEl) this.setPinned(null);
    });

    const zoomBehavior = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.3, 4])
      .on('zoom', (event) => this.zoomLayer.attr('transform', event.transform));
    this.svg.call(zoomBehavior).call(zoomBehavior.transform, zoomIdentity);

    this.simulation.on('tick', () => this.onTick());
  }

  setData(file: RequirementsGraphFile): void {
    this.fullNodes = file.nodes;
    this.fullLinks = file.links;
    this.nodesById = new Map(file.nodes.map((n) => [n.id, n]));

    this.profileOrder = [];
    for (const n of file.nodes) {
      if (n.layer === 'profile' && !this.profileOrder.includes(n.id)) this.profileOrder.push(n.id);
    }
    this.profileCount = Math.max(this.profileOrder.length, 1);

    this.drawBands();
    this.rebuild();
  }

  resize(width: number, height: number): void {
    this.width = Math.max(width, 200);
    this.height = Math.max(height, 200);
    this.xScale.range([150, this.width - 150]);
    this.drawBands();
    // Both forces must be REASSIGNED (not just left in place expecting them to pick up the
    // new width/height): d3-force evaluates a force's accessor function once, when it's
    // (re)initialized via `.force(name, ...)` or `.nodes(...)`, and caches the result — it
    // does NOT re-call the accessor on every tick. Leaving the old force objects in place
    // after a resize would keep pulling nodes toward targets computed from the *previous*
    // width/height forever, however many ticks run.
    this.applyGeometryForces();
    // Synchronous re-settle to the new dimensions rather than an animated restart: an
    // animated restart can get caught by a low residual alpha from the previous settle and
    // never actually reach the new (larger) targets, leaving the graph visibly squashed
    // into whatever size it first rendered at.
    this.settle(150);
  }

  setCollapseRules(collapsed: boolean): void {
    if (this.collapseRules === collapsed) return;
    this.collapseRules = collapsed;
    this.rebuild();
  }

  setSearchTerm(term: string): void {
    this.searchTerm = term.trim().toLowerCase();
    this.applyHighlight();
  }

  setPinned(id: string | null): void {
    this.pinnedId = id;
    this.callbacks.onSelect(id ? (this.nodesById.get(id) ?? null) : null);
    this.applyHighlight();
  }

  reset(): void {
    this.pinnedId = null;
    this.hoveredId = null;
    this.searchTerm = '';
    this.callbacks.onSelect(null);
    this.applyHighlight();
  }

  destroy(): void {
    this.simulation.stop();
  }

  // -- internals ------------------------------------------------------------

  private visibleNodes(): GraphNode[] {
    return this.collapseRules ? this.fullNodes.filter((n) => n.layer !== 'rule') : this.fullNodes;
  }

  private visibleLinks(visibleIds: Set<string>): GraphLinkRaw[] {
    return this.fullLinks.filter((l) => visibleIds.has(l.source) && visibleIds.has(l.target));
  }

  private rebuild(): void {
    const visible = this.visibleNodes();
    const visibleIds = new Set(visible.map((n) => n.id));
    const links = this.visibleLinks(visibleIds);

    this.yFraction = this.computeYFractions(visible);

    // Seed every unpositioned node directly at its deterministic target (± a hair of jitter
    // to break exact ties) — the target itself already guarantees non-overlapping spacing,
    // so the warm-up below only has to settle x/collide/link forces, not fight through a
    // dense central clump.
    this.simNodes = visible.map((n) => {
      const prev = this.positionById.get(n.id);
      const seed: SimNode = { ...n } as SimNode;
      seed.x = prev?.x ?? this.xScale(n.layer) ?? this.width / 2;
      seed.y = prev?.y ?? this.bandCenter(n) + (Math.random() - 0.5) * 4;
      return seed;
    });
    this.positionById = new Map(this.simNodes.map((n) => [n.id, n]));
    this.simLinks = links.map((l) => ({ source: l.source, target: l.target, kind: l.kind }));

    this.simulation
      .nodes(this.simNodes)
      .force('collide', forceCollide<SimNode>((d) => this.radius(d) + 3).iterations(2))
      .force(
        'link',
        forceLink<SimNode, SimLink>(this.simLinks)
          .id((d) => d.id)
          .distance(60)
          .strength(0.04),
      );
    this.applyGeometryForces();

    this.drawLinks();
    this.drawNodes();
    // Pre-converge synchronously so the very first paint is already settled — an animated
    // restart alone can get interrupted by an early ResizeObserver firing and leave nodes
    // visibly clumped near their seed point.
    this.settle(250);
    this.applyHighlight();
  }

  /** (Re)assigns the x/y forces so they re-initialize against the current width/height and
   *  yFraction targets — see the comment in resize() for why this can't just be left alone. */
  private applyGeometryForces(): void {
    this.simulation
      .force('x', forceX<SimNode>((d) => this.xScale(d.layer) ?? this.width / 2).strength(0.9))
      .force('y', forceY<SimNode>((d) => this.bandCenter(d)).strength((d) => yStrength(d.layer)));
  }

  /** Run the simulation to (near-)equilibrium synchronously, paint it, then hand off to a
   *  low-energy animated restart so drag interactions still feel alive. */
  private settle(ticks: number): void {
    this.simulation.alpha(1).stop();
    for (let i = 0; i < ticks; i++) this.simulation.tick();
    this.onTick();
    this.simulation.alphaTarget(0).restart();
  }

  private computeYFractions(visible: GraphNode[]): Map<string, number> {
    const byLayer = new Map<GraphLayer, GraphNode[]>();
    for (const n of visible) (byLayer.get(n.layer) ?? byLayer.set(n.layer, []).get(n.layer)!).push(n);

    const flat = (layer: GraphLayer) => stackedFractions(byLayer.get(layer) ?? [], () => '', ['']);
    const fractions = new Map<string, number>([
      ...flat('source'),
      ...flat('branch'),
      ...flat('profile'),
      ...stackedFractions(
        byLayer.get('rule') ?? [],
        (n) => (isRuleNode(n) ? profileNodeId(n.profileId) : ''),
        this.profileOrder,
        0.015,
      ),
      ...stackedFractions(
        byLayer.get('deliverable') ?? [],
        (n) => (n.layer === 'deliverable' ? n.kind : ''),
        KIND_ORDER,
        0.02,
      ),
    ]);
    return fractions;
  }

  private bandCenter(node: GraphNode): number {
    return (this.yFraction.get(node.id) ?? 0.5) * this.height;
  }

  private radius(node: GraphNode): number {
    if (node.layer === 'rule') return 4;
    if (node.layer === 'source') return 11;
    if (node.layer === 'branch') return 9;
    if (node.layer === 'profile') return 9 + Math.min(node.ruleCount, 40) * 0.25;
    return node.isOutcome ? 11 : 8;
  }

  private fillVar(layer: GraphLayer): string {
    return `var(--layer-${layer})`;
  }

  private drawBands(): void {
    const bands = Array.from({ length: this.profileCount }, (_, i) => i);
    this.bandLayer
      .selectAll<SVGRectElement, number>('rect')
      .data(bands)
      .join('rect')
      .attr('x', 0)
      .attr('width', this.width)
      .attr('y', (i) => (i / this.profileCount) * this.height)
      .attr('height', this.height / this.profileCount)
      .attr('fill', (i) => (i % 2 === 0 ? 'var(--band-a)' : 'var(--band-b)'));

    const cols = LAYER_ORDER.map((layer) => ({ layer, x: this.xScale(layer) ?? 0 }));
    this.bandLayer
      .selectAll<SVGTextElement, (typeof cols)[number]>('text.col-label')
      .data(cols)
      .join('text')
      .attr('class', 'col-label')
      .attr('x', (d) => d.x)
      .attr('y', 16)
      .attr('text-anchor', 'middle')
      .text((d) => columnLabel(d.layer));
  }

  private drawLinks(): void {
    this.linkLayer
      .selectAll<SVGPathElement, SimLink>('path')
      .data(this.simLinks, (d: SimLink) => `${idOf(d.source)}->${idOf(d.target)}`)
      .join('path')
      .attr('class', (d) => `link link--${d.kind}`);
  }

  private drawNodes(): void {
    const dragBehavior = drag<SVGGElement, SimNode>()
      .on('start', (event, d) => {
        if (!event.active) this.simulation.alphaTarget(0.1).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on('drag', (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on('end', (event, d) => {
        if (!event.active) this.simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    const groups = this.nodeLayer
      .selectAll<SVGGElement, SimNode>('g.node')
      .data(this.simNodes, (d: SimNode) => d.id)
      .join((enter) => {
        const g = enter
          .append('g')
          .attr('class', (d) => `node node--${d.layer}`)
          .call(dragBehavior)
          .on('mouseenter', (_event, d) => this.setHover(d.id))
          .on('mouseleave', () => this.setHover(null))
          .on('click', (event, d) => {
            event.stopPropagation();
            this.setPinned(this.pinnedId === d.id ? null : d.id);
          });
        g.append('title');
        g.each((d, i, nodesArr) => this.appendShape(select(nodesArr[i]), d));
        return g;
      });

    groups.select('title').text((d) => tooltipFor(d));
  }

  private appendShape(g: Selection<SVGGElement, SimNode, null, undefined>, d: SimNode): void {
    if (d.layer === 'rule') {
      g.append('circle').attr('r', this.radius(d)).attr('class', 'shape');
    } else if (d.layer === 'deliverable') {
      const r = this.radius(d);
      g.append('rect')
        .attr('class', 'shape')
        .attr('x', -r)
        .attr('y', -r)
        .attr('width', r * 2)
        .attr('height', r * 2)
        .attr('rx', 3)
        .attr('transform', 'rotate(45)');
      g.append('text')
        .attr('class', 'label')
        .attr('x', r + 6)
        .attr('dy', 4)
        .text(truncateLabel(d.label));
    } else {
      const r = this.radius(d);
      const w = d.layer === 'profile' ? r * 2.6 : r * 2.2;
      g.append('rect')
        .attr('class', 'shape')
        .attr('x', -w / 2)
        .attr('y', -r)
        .attr('width', w)
        .attr('height', r * 2)
        .attr('rx', d.layer === 'branch' ? r : 4);
      if (d.layer === 'profile') {
        g.append('text').attr('class', 'badge').attr('y', 4).text(d.ruleCount);
      }
      const side = d.layer === 'source' ? -(w / 2 + 6) : w / 2 + 6;
      g.append('text')
        .attr('class', 'label')
        .attr('x', side)
        .attr('dy', 4)
        .attr('text-anchor', d.layer === 'source' ? 'end' : 'start')
        // Sources have long official titles that are indistinguishable once truncated to fit
        // a node label — show the short slug instead, full title stays in the tooltip and the
        // side panel.
        .text(d.layer === 'source' ? sourceShortId(d.id) : truncateLabel(d.label, 28));
    }
    this.styleShape(g, d);
  }

  private styleShape(g: Selection<SVGGElement, SimNode, null, undefined>, d: SimNode): void {
    const shape = g.select<SVGElement>('.shape');
    shape.attr('fill', this.fillVar(d.layer));
    if (d.layer === 'rule') {
      shape
        .attr('fill', `var(--severity-${d.severity})`)
        .attr('stroke-dasharray', dashForVerification(d.verification))
        .classed('pending', d.pending);
    }
    if (d.layer === 'profile') shape.classed('draft', d.status === 'draft');
    if (d.layer === 'source') shape.classed('superseded', d.status !== 'active');
    if (d.layer === 'deliverable') shape.classed('outcome', d.isOutcome);
  }

  private onTick(): void {
    this.linkLayer.selectAll<SVGPathElement, SimLink>('path').attr('d', (d) => linkPath(d));
    this.nodeLayer
      .selectAll<SVGGElement, SimNode>('g.node')
      .attr('transform', (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
  }

  private setHover(id: string | null): void {
    this.hoveredId = id;
    this.applyHighlight();
  }

  private applyHighlight(): void {
    const activeId = this.hoveredId ?? this.pinnedId;
    const visibleIds = new Set(this.simNodes.map((n) => n.id));

    let activeNodeIds: Set<string> | null = null;
    const edgePairs: Array<[string, string]> = [];

    if (activeId) {
      const startNode = this.nodesById.get(activeId);
      const direction = startNode ? directionForLayer(startNode.layer) : 'both';
      const reach = computeReachable(this.fullNodes, this.fullLinks, activeId, direction);
      activeNodeIds = projectToVisible(reach.nodeIds, this.nodesById, visibleIds);
      const pairSeen = new Set<string>();
      for (const idx of reach.linkIndexes) {
        const link = this.fullLinks[idx];
        const a = this.toVisibleId(link.source, visibleIds);
        const b = this.toVisibleId(link.target, visibleIds);
        if (!a || !b || a === b) continue;
        const key = `${a}->${b}`;
        if (pairSeen.has(key)) continue;
        pairSeen.add(key);
        edgePairs.push([a, b]);
      }
    } else if (this.searchTerm) {
      activeNodeIds = new Set();
      for (const n of this.fullNodes) {
        if (matchesSearch(n, this.searchTerm)) {
          const vis = this.toVisibleId(n.id, visibleIds);
          if (vis) activeNodeIds.add(vis);
        }
      }
    }

    this.nodeLayer
      .selectAll<SVGGElement, SimNode>('g.node')
      .classed('is-dim', (d) => activeNodeIds !== null && !activeNodeIds.has(d.id))
      .classed('is-active', (d) => activeNodeIds !== null && activeNodeIds.has(d.id));

    this.linkLayer
      .selectAll<SVGPathElement, SimLink>('path')
      .classed('is-dim', () => activeNodeIds !== null);

    this.highlightLayer
      .selectAll<SVGPathElement, [string, string]>('path')
      .data(edgePairs, (d: [string, string]) => `${d[0]}->${d[1]}`)
      .join('path')
      .attr('class', 'highlight-edge')
      .attr('d', ([a, b]) => {
        const from = this.positionById.get(a);
        const to = this.positionById.get(b);
        if (!from || !to) return null;
        return linkPath({ source: from, target: to, kind: 'impacts' });
      });
  }

  private toVisibleId(id: string, visibleIds: Set<string>): string | null {
    if (visibleIds.has(id)) return id;
    const node = this.nodesById.get(id);
    if (node?.layer === 'rule') {
      const profileId = profileNodeId(node.profileId);
      if (visibleIds.has(profileId)) return profileId;
    }
    return null;
  }
}

function idOf(end: SimLink['source']): string {
  return typeof end === 'string' ? end : ((end as SimNode).id ?? '');
}

function dashForVerification(v: string): string {
  switch (v) {
    case 'automatic':
      return '0';
    case 'expert_evidence':
      return '3,2';
    case 'external_fact':
      return '1,2';
    case 'external_verdict':
      return '5,2,1,2';
    default:
      return '0';
  }
}

function columnLabel(layer: GraphLayer): string {
  switch (layer) {
    case 'source':
      return 'Источники';
    case 'branch':
      return 'Разделы';
    case 'profile':
      return 'Профили';
    case 'rule':
      return 'Требования';
    case 'deliverable':
      return 'Артефакты';
  }
}

function tooltipFor(d: GraphNode): string {
  if (isRuleNode(d)) {
    return `${d.label}\n${d.category} · ${d.severity} · ${d.verification}${d.pending ? ' · ожидает' : ''}`;
  }
  if (d.layer === 'source') return `${d.label}\n${d.status}, transcribed: ${d.transcribed}`;
  if (d.layer === 'branch') return `${d.label}\nправил: ${d.ruleCount}`;
  if (d.layer === 'profile') return `${d.label}\n${d.status}, правил: ${d.ruleCount}`;
  return `${d.label}\n${d.kind}${d.isOutcome ? ' · итоговый результат' : ''}`;
}
