import { profileNodeId, type GraphLinkRaw, type GraphNode } from './graph.types';

export type Direction = 'down' | 'up' | 'both';

interface Adjacency {
  forward: Map<string, GraphLinkRaw[]>;
  backward: Map<string, GraphLinkRaw[]>;
}

export function buildAdjacency(links: GraphLinkRaw[]): Adjacency {
  const forward = new Map<string, GraphLinkRaw[]>();
  const backward = new Map<string, GraphLinkRaw[]>();
  for (const link of links) {
    (forward.get(link.source) ?? forward.set(link.source, []).get(link.source)!).push(link);
    (backward.get(link.target) ?? backward.set(link.target, []).get(link.target)!).push(link);
  }
  return { forward, backward };
}

/** Layer-appropriate default highlight direction for a hovered/clicked node. */
export function directionForLayer(layer: GraphNode['layer']): Direction {
  if (layer === 'deliverable') return 'up';
  if (layer === 'rule') return 'both';
  return 'down';
}

export interface HighlightResult {
  nodeIds: Set<string>;
  linkIndexes: Set<number>;
}

/**
 * BFS from `startId` following link direction (or both). Used for both the
 * "change ripples down to deliverables" and "deliverable traces back up" scenarios —
 * one generic walk covers both, since which edge kinds exist between two layers
 * already constrains what gets reached.
 */
export function computeReachable(
  nodes: GraphNode[],
  links: GraphLinkRaw[],
  startId: string,
  direction: Direction,
): HighlightResult {
  const { forward, backward } = buildAdjacency(links);
  const indexOfLink = new Map(links.map((link, index) => [link, index]));
  const nodeIds = new Set<string>([startId]);
  const linkIndexes = new Set<number>();

  const walk = (from: string, adjacency: Map<string, GraphLinkRaw[]>) => {
    const queue = [from];
    const seen = new Set<string>([from]);
    while (queue.length) {
      const current = queue.shift()!;
      for (const link of adjacency.get(current) ?? []) {
        const next = adjacency === forward ? link.target : link.source;
        linkIndexes.add(indexOfLink.get(link)!);
        nodeIds.add(next);
        if (!seen.has(next)) {
          seen.add(next);
          queue.push(next);
        }
      }
    }
  };

  if (direction === 'down' || direction === 'both') walk(startId, forward);
  if (direction === 'up' || direction === 'both') walk(startId, backward);

  return { nodeIds, linkIndexes };
}

/**
 * Projects a highlight computed on the full graph onto the currently visible
 * node set: a highlighted rule that is hidden (collapsed into its profile)
 * lights up its owning profile bubble instead, so the "which requirements are
 * affected" story still reads with rules collapsed.
 */
export function projectToVisible(
  nodeIds: Set<string>,
  nodesById: Map<string, GraphNode>,
  visibleIds: Set<string>,
): Set<string> {
  const projected = new Set<string>();
  for (const id of nodeIds) {
    if (visibleIds.has(id)) {
      projected.add(id);
      continue;
    }
    const node = nodesById.get(id);
    if (node?.layer === 'rule') {
      const profileId = profileNodeId(node.profileId);
      if (visibleIds.has(profileId)) projected.add(profileId);
    }
  }
  return projected;
}
