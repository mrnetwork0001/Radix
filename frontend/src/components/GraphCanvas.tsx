/**
 * The dependency graph, rendered as an interactive force-directed canvas.
 *
 * Three constraints shape almost every decision in this file:
 *
 *  1. **react-force-graph mutates the objects it is given.** It writes `x`, `y`,
 *     `vx`, `vy` onto each node and replaces a link's `source`/`target` ids with
 *     the node objects themselves. Rebuilding those objects on a re-render would
 *     throw away the settled layout and restart the simulation, so the transform
 *     is memoised on `data` identity alone - never on the animation props, which
 *     change many times per breach.
 *
 *  2. **The paint callbacks run per node, per frame.** At ~500 nodes and 60fps
 *     that is 30k calls/second, so they allocate nothing: colours are
 *     pre-stringified in the transform, per-frame values (font, zoom, clock) are
 *     computed once in `onRenderFramePre`, and the soft radial glow is a cached
 *     sprite blitted with `drawImage` rather than a `createRadialGradient` call
 *     rebuilt for every node on every frame.
 *
 *  3. **Animation state never goes through `setState`.** The pulsing halo and
 *     the travelling dash read a ref updated by a `requestAnimationFrame` loop.
 *     A state update per frame would re-render the whole component tree 60 times
 *     a second and stall the force simulation.
 *
 * One graph-direction subtlety is worth stating plainly: `DEPENDS_ON` points
 * *dependent → dependency*, but an infection travels *dependency → dependent* -
 * the opposite way along the same edge. Infection particles therefore run with a
 * negative `linkDirectionalParticleSpeed`, which force-graph supports natively
 * (it seeds `__progressRatio` at 1 and wraps below 0), rather than by inventing
 * reversed duplicate links.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { ForceGraphMethods } from 'react-force-graph-2d';

import type { GraphEdge, GraphNode, NodeLabel } from '../lib/types';
import type { GraphPayload } from '../hooks/useGraphData';

export type { GraphPayload };

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------
// Mirrors the `--*` tokens in src/index.css. The canvas cannot read Tailwind
// classes or resolve CSS custom properties cheaply per frame, so the literals
// live here as pre-built rgba strings - building them per node per frame is a
// measurable cost at 500 nodes.

const RGB: Record<string, readonly [number, number, number]> = {
  cyan: [34, 211, 238], // Package
  slate: [148, 163, 184], // Version
  violet: [167, 139, 250], // Maintainer
  green: [74, 222, 128], // Service
  amber: [251, 191, 36], // Lockfile
  red: [244, 63, 94], // compromised / infected
};

const rgba = (key: string, alpha: number): string => {
  const c = RGB[key] ?? RGB.slate!;
  return `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${alpha})`;
};

const solid = (key: string): string => rgba(key, 1);

/** Per-label draw kit, resolved once in the transform. */
interface LabelStyle {
  key: string;
  body: string;
  rim: string;
  baseRadius: number;
}

const LABEL_STYLES: Record<NodeLabel, LabelStyle> = {
  Package: { key: 'cyan', body: rgba('cyan', 0.92), rim: solid('cyan'), baseRadius: 2.4 },
  Version: { key: 'slate', body: rgba('slate', 0.8), rim: rgba('slate', 0.95), baseRadius: 1.8 },
  Maintainer: { key: 'violet', body: rgba('violet', 0.92), rim: solid('violet'), baseRadius: 3.0 },
  Service: { key: 'green', body: rgba('green', 0.92), rim: solid('green'), baseRadius: 3.6 },
  Lockfile: { key: 'amber', body: rgba('amber', 0.92), rim: solid('amber'), baseRadius: 2.8 },
};

const FALLBACK_STYLE: LabelStyle = {
  key: 'slate',
  body: rgba('slate', 0.7),
  rim: rgba('slate', 0.9),
  baseRadius: 2.0,
};

const INFECTED_BODY = rgba('red', 0.95);
const INFECTED_RIM = solid('red');
const ROOT_HALO = rgba('red', 0.85);
const WAVEFRONT_RING = rgba('red', 0.9);
const SELECT_RING = 'rgba(255, 255, 255, 0.95)';
const SELECT_RING_OUTER = rgba('cyan', 0.85);
const TEXT = 'rgba(232, 238, 247, 0.95)';
const TEXT_DIM = 'rgba(232, 238, 247, 0.55)';
const TEXT_SHADOW = 'rgba(6, 9, 15, 0.9)';

const LINK_BASE = 'rgba(148, 163, 184, 0.16)';
const LINK_TYPOSQUAT = rgba('amber', 0.34);
const LINK_MAINTAINED = rgba('violet', 0.22);
const LINK_RESOLVED = rgba('slate', 0.2);
const LINK_DIM = 'rgba(148, 163, 184, 0.05)';
const LINK_RELATED = rgba('cyan', 0.55);
const LINK_INFECTED = rgba('red', 0.75);
const INFECT_PARTICLE = rgba('red', 0.95);
const INFECT_DASH = rgba('red', 0.5);

// ---------------------------------------------------------------------------
// Tuning
// ---------------------------------------------------------------------------

/** Zoom level above which ordinary node labels appear. Below it the canvas would be a wall of text. */
const LABEL_ZOOM = 1.45;
/** Important nodes earn their label earlier. */
const LABEL_ZOOM_IMPORTANT = 0.75;
/** Glow sprite radius as a multiple of the node radius. */
const GLOW_SCALE = 3.4;
/** Duration of a node's ignition flash, ms. */
const IGNITION_MS = 620;
const DIM_ALPHA = 0.14;
const SPRITE_PX = 64;
const TAU = Math.PI * 2;

// ---------------------------------------------------------------------------
// Graph model
// ---------------------------------------------------------------------------

/**
 * A node as the canvas holds it. `x`/`y`/`vx`/`vy` are absent here because
 * force-graph adds them by mutation - declared optional so the paint code can
 * read them without a cast.
 */
interface RadixNode {
  id: number;
  name: string;
  kind: NodeLabel;
  style: LabelStyle;
  /** Draw radius in graph units, already folded with importance. */
  radius: number;
  /** 0–1, from weekly downloads and dependent count. */
  importance: number;
  compromised: boolean;
  /** Pre-built tooltip HTML; recomputing it per hover would be wasteful. */
  tooltip: string;
  raw: GraphNode;
  x?: number;
  y?: number;
}

interface RadixLink {
  source: number | RadixNode;
  target: number | RadixNode;
  kind: GraphEdge['type'];
  color: string;
  width: number;
  raw: GraphEdge;
}

interface GraphModel {
  nodes: RadixNode[];
  links: RadixLink[];
  /** id → directly connected ids, for selection dimming. */
  neighbours: Map<number, Set<number>>;
  byId: Map<number, RadixNode>;
}

const EMPTY_MODEL: GraphModel = {
  nodes: [],
  links: [],
  neighbours: new Map(),
  byId: new Map(),
};

/** force-graph swaps endpoint ids for node objects once the simulation starts. */
function endpointId(end: number | RadixNode | undefined): number {
  if (typeof end === 'number') return end;
  return end?.id ?? -1;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function buildTooltip(node: GraphNode): string {
  const rows: string[] = [];
  const add = (k: string, v: string | number | undefined) => {
    if (v === undefined || v === '') return;
    rows.push(
      `<div style="opacity:.62">${escapeHtml(k)}</div><div>${escapeHtml(String(v))}</div>`,
    );
  };

  switch (node.label) {
    case 'Package':
      add('ecosystem', node.ecosystem);
      add('downloads/wk', node.downloads_weekly?.toLocaleString());
      if (node.risk_score !== undefined) add('risk', node.risk_score.toFixed(2));
      break;
    case 'Version':
      add('semver', node.semver);
      add('published', node.published_at?.slice(0, 10));
      break;
    case 'Maintainer':
      add('username', node.username);
      add('key', node.key_fingerprint);
      break;
    case 'Service':
      add('criticality', node.criticality);
      add('repo', node.repo_url);
      break;
    case 'Lockfile':
      add('file', node.filename);
      add('commit', node.commit_hash?.slice(0, 10));
      break;
    default:
      break;
  }

  return (
    `<div style="font:12px Inter,system-ui,sans-serif;padding:8px 10px;border-radius:10px;` +
    `background:rgba(6,9,15,.92);border:1px solid rgba(255,255,255,.12);color:#e8eef7;` +
    `box-shadow:0 8px 24px -8px rgba(0,0,0,.8)">` +
    `<div style="font-weight:600;margin-bottom:${rows.length ? '4px' : '0'}">${escapeHtml(node.name)}` +
    (node.is_compromised ? ` <span style="color:#f43f5e">COMPROMISED</span>` : '') +
    `</div>` +
    `<div style="opacity:.5;font-size:10px;letter-spacing:.08em;text-transform:uppercase">${escapeHtml(node.label)}</div>` +
    (rows.length
      ? `<div style="display:grid;grid-template-columns:auto auto;gap:2px 10px;margin-top:6px">${rows.join('')}</div>`
      : '') +
    `</div>`
  );
}

/**
 * Turn the API payload into force-graph's shape.
 *
 * Runs once per payload. Every value the paint loop needs - colour strings,
 * radius, tooltip - is resolved here so the per-frame callbacks only read.
 */
function buildModel(payload: GraphPayload | null): GraphModel {
  if (!payload || payload.nodes.length === 0) return EMPTY_MODEL;

  // Importance is driven by how many things depend on a node, which is the
  // in-degree of DEPENDS_ON - the same quantity the blast radius measures.
  const dependents = new Map<number, number>();
  for (const edge of payload.edges) {
    if (edge.type !== 'DEPENDS_ON') continue;
    dependents.set(edge.target, (dependents.get(edge.target) ?? 0) + 1);
  }

  let maxDownloads = 0;
  let maxDependents = 0;
  for (const node of payload.nodes) {
    if (node.downloads_weekly && node.downloads_weekly > maxDownloads) {
      maxDownloads = node.downloads_weekly;
    }
  }
  for (const count of dependents.values()) {
    if (count > maxDependents) maxDependents = count;
  }

  const logMaxDownloads = Math.log1p(maxDownloads);
  const logMaxDependents = Math.log1p(maxDependents);

  const byId = new Map<number, RadixNode>();
  const nodes: RadixNode[] = payload.nodes.map((raw) => {
    const style = LABEL_STYLES[raw.label] ?? FALLBACK_STYLE;

    // Log scaling: weekly downloads span several orders of magnitude, and a
    // linear map would render every package but the top few as the same dot.
    const downloadScore =
      logMaxDownloads > 0 ? Math.log1p(raw.downloads_weekly ?? 0) / logMaxDownloads : 0;
    const dependentScore =
      logMaxDependents > 0 ? Math.log1p(dependents.get(raw.id) ?? 0) / logMaxDependents : 0;
    const importance = Math.min(1, downloadScore * 0.55 + dependentScore * 0.65);

    const node: RadixNode = {
      id: raw.id,
      name: raw.name,
      kind: raw.label,
      style,
      radius: style.baseRadius + importance * 5.2,
      importance,
      compromised: raw.is_compromised === true,
      tooltip: buildTooltip(raw),
      raw,
    };
    byId.set(raw.id, node);
    return node;
  });

  const neighbours = new Map<number, Set<number>>();
  const link = (a: number, b: number) => {
    let set = neighbours.get(a);
    if (!set) {
      set = new Set<number>();
      neighbours.set(a, set);
    }
    set.add(b);
  };

  const links: RadixLink[] = [];
  for (const edge of payload.edges) {
    // Drop dangling edges: force-graph throws when an endpoint is missing, and
    // `?limit=` on /api/graph/full can legitimately truncate one side away.
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue;

    let color = LINK_BASE;
    let width = 0.7;
    if (edge.type === 'TYPOSQUAT_OF') {
      color = LINK_TYPOSQUAT;
      width = 1.1;
    } else if (edge.type === 'MAINTAINED_BY') {
      color = LINK_MAINTAINED;
    } else if (edge.type === 'RESOLVED_IN') {
      color = LINK_RESOLVED;
    }

    links.push({ source: edge.source, target: edge.target, kind: edge.type, color, width, raw: edge });
    link(edge.source, edge.target);
    link(edge.target, edge.source);
  }

  return { nodes, links, neighbours, byId };
}

// ---------------------------------------------------------------------------
// Glow sprite cache
// ---------------------------------------------------------------------------

/**
 * Soft radial glows, pre-rendered once per colour.
 *
 * `createRadialGradient` + `fill` per node per frame is the single most
 * expensive thing this canvas could do. Baking each glow into a small offscreen
 * canvas turns it into a `drawImage` blit, and intensity is applied with
 * `globalAlpha` so one sprite serves every brightness.
 */
const glowSprites = new Map<string, HTMLCanvasElement>();

function glowSprite(colorKey: string): HTMLCanvasElement | null {
  const cached = glowSprites.get(colorKey);
  if (cached) return cached;
  if (typeof document === 'undefined') return null;

  const canvas = document.createElement('canvas');
  canvas.width = SPRITE_PX;
  canvas.height = SPRITE_PX;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const mid = SPRITE_PX / 2;
  const gradient = ctx.createRadialGradient(mid, mid, 0, mid, mid, mid);
  gradient.addColorStop(0, rgba(colorKey, 0.55));
  gradient.addColorStop(0.35, rgba(colorKey, 0.22));
  gradient.addColorStop(0.7, rgba(colorKey, 0.06));
  gradient.addColorStop(1, rgba(colorKey, 0));
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, SPRITE_PX, SPRITE_PX);

  glowSprites.set(colorKey, canvas);
  return canvas;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface GraphCanvasProps {
  data: GraphPayload | null;
  infectedIds: Set<number>;
  infectionPaths: number[][];
  rootId: number | null;
  selectedId: number | null;
  onSelectNode: (id: number | null) => void;
  isSimulating: boolean;
}

/** Values recomputed once per frame rather than once per node. */
interface FrameState {
  clock: number;
  scale: number;
  labelFont: string;
  showLabels: boolean;
  showImportantLabels: boolean;
}

export function GraphCanvas({
  data,
  infectedIds,
  infectionPaths,
  rootId,
  selectedId,
  onSelectNode,
  isSimulating,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<RadixNode, RadixLink> | undefined>(undefined);

  const [size, setSize] = useState({ width: 0, height: 0 });
  const [hoverId, setHoverId] = useState<number | null>(null);

  // --- Layout ------------------------------------------------------------

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const apply = (width: number, height: number) => {
      setSize((previous) =>
        previous.width === width && previous.height === height
          ? previous // identical object → no re-render, no ResizeObserver feedback loop
          : { width, height },
      );
    };

    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) apply(Math.round(rect.width), Math.round(rect.height));
    });
    observer.observe(element);
    apply(Math.round(element.clientWidth), Math.round(element.clientHeight));

    return () => observer.disconnect();
  }, []);

  // --- Model -------------------------------------------------------------

  // Keyed on `data` identity only. Adding an animation prop here would rebuild
  // every node object mid-breach and reset the layout to a random cloud.
  const model = useMemo(() => buildModel(data), [data]);

  const graphData = useMemo(
    () => ({ nodes: model.nodes, links: model.links }),
    [model],
  );

  // --- Infection topology ------------------------------------------------

  /** Directed `a|b` pairs meaning "infection flows a → b". */
  const infectionPairs = useMemo(() => {
    const pairs = new Set<string>();
    for (const path of infectionPaths) {
      for (let i = 0; i < path.length - 1; i += 1) {
        const from = path[i];
        const to = path[i + 1];
        if (from === undefined || to === undefined) continue;
        pairs.add(`${from}|${to}`);
      }
    }
    return pairs;
  }, [infectionPaths]);

  /** Hop distance from the root, for the wavefront highlight. */
  const depthById = useMemo(() => {
    const depths = new Map<number, number>();
    if (rootId !== null) depths.set(rootId, 0);
    for (const path of infectionPaths) {
      if (path.length === 0) continue;
      const offset = rootId !== null && path[0] === rootId ? 0 : 1;
      for (let i = 0; i < path.length; i += 1) {
        const id = path[i];
        if (id === undefined) continue;
        const depth = i + offset;
        const known = depths.get(id);
        if (known === undefined || depth < known) depths.set(id, depth);
      }
    }
    return depths;
  }, [infectionPaths, rootId]);

  /** Outermost hop currently alight - those nodes get the leading-edge flare. */
  const wavefrontDepth = useMemo(() => {
    let deepest = -1;
    for (const id of infectedIds) {
      const depth = depthById.get(id);
      if (depth !== undefined && depth > deepest) deepest = depth;
    }
    return deepest;
  }, [infectedIds, depthById]);

  /** Ids highlighted while a selection is active: the node plus its direct neighbours. */
  const relatedIds = useMemo(() => {
    if (selectedId === null) return null;
    const set = new Set<number>([selectedId]);
    for (const id of model.neighbours.get(selectedId) ?? []) set.add(id);
    return set;
  }, [selectedId, model]);

  // --- Animation clock ---------------------------------------------------

  // A ref, not state: the pulse must not re-render React 60 times a second.
  const clockRef = useRef(0);
  useEffect(() => {
    let handle = 0;
    const tick = (timestamp: number) => {
      clockRef.current = timestamp;
      handle = requestAnimationFrame(tick);
    };
    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, []);

  /** When each node first became infected, so it can flash on arrival. */
  const ignitedAtRef = useRef(new Map<number, number>());
  useEffect(() => {
    const ignited = ignitedAtRef.current;
    if (infectedIds.size === 0) {
      ignited.clear();
      return;
    }
    const now = performance.now();
    for (const id of infectedIds) {
      if (!ignited.has(id)) ignited.set(id, now);
    }
    for (const id of Array.from(ignited.keys())) {
      if (!infectedIds.has(id)) ignited.delete(id);
    }
  }, [infectedIds]);

  // Per-frame values, computed once in `onRenderFramePre` instead of once per
  // node. A ref rather than state: this updates every frame.
  const frameRef = useRef<FrameState>({
    clock: 0,
    scale: 1,
    labelFont: '12px Inter, system-ui, sans-serif',
    showLabels: false,
    showImportantLabels: false,
  });

  const onRenderFramePre = useCallback((_ctx: CanvasRenderingContext2D, scale: number) => {
    const frame = frameRef.current;
    frame.clock = clockRef.current || performance.now();
    frame.scale = scale;
    // Constant on-screen text size: divide out the zoom so labels neither
    // vanish nor swallow the viewport.
    frame.labelFont = `${(11 / scale).toFixed(2)}px Inter, system-ui, sans-serif`;
    frame.showLabels = scale >= LABEL_ZOOM;
    frame.showImportantLabels = scale >= LABEL_ZOOM_IMPORTANT;
  }, []);

  // --- Node painting -----------------------------------------------------

  const paintNode = useCallback(
    (node: RadixNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const { x, y } = node;
      if (x === undefined || y === undefined) return;

      const frame = frameRef.current;
      const infected = infectedIds.has(node.id);
      const isRoot = rootId === node.id;
      const isSelected = selectedId === node.id;
      const isHovered = hoverId === node.id;
      const dimmed = relatedIds !== null && !relatedIds.has(node.id) && !infected && !isRoot;
      // Nodes on the outermost lit hop, i.e. where the infection is arriving now.
      const onWavefront =
        infected && isSimulating && depthById.get(node.id) === wavefrontDepth && !isRoot;

      const style = node.style;
      const colorKey = infected || node.compromised ? 'red' : style.key;

      // Ignition flash: a short swell + shockwave the moment a node is reached.
      let swell = 0;
      let ignitionAge = 1;
      if (infected) {
        const ignitedAt = ignitedAtRef.current.get(node.id);
        if (ignitedAt !== undefined) {
          ignitionAge = Math.min(1, (frame.clock - ignitedAt) / IGNITION_MS);
          swell = Math.sin(ignitionAge * Math.PI) * 0.75;
        }
      }

      const radius = node.radius * (1 + swell * 0.45);
      const alpha = dimmed ? DIM_ALPHA : 1;

      ctx.globalAlpha = alpha;

      // --- Soft radial glow (cached sprite, not a per-frame gradient) ---
      const sprite = glowSprite(colorKey);
      if (sprite) {
        const glowRadius = radius * GLOW_SCALE * (infected ? 1.25 : 1);
        const intensity =
          (infected ? 0.85 : 0.4 + node.importance * 0.35) *
          (isHovered || isSelected ? 1.25 : 1) *
          alpha;
        ctx.globalAlpha = Math.min(1, intensity);
        ctx.drawImage(sprite, x - glowRadius, y - glowRadius, glowRadius * 2, glowRadius * 2);
        ctx.globalAlpha = alpha;
      }

      // --- Compromised root: two offset pulsing halos ---
      if (isRoot) {
        const period = 1900;
        for (let ring = 0; ring < 2; ring += 1) {
          const phase = ((frame.clock + ring * period * 0.5) % period) / period;
          const ringRadius = radius * (1.35 + phase * 2.6);
          ctx.globalAlpha = (1 - phase) * 0.75 * alpha;
          ctx.strokeStyle = ROOT_HALO;
          ctx.lineWidth = 1.6 / scale;
          ctx.beginPath();
          ctx.arc(x, y, ringRadius, 0, TAU);
          ctx.stroke();
        }
        ctx.globalAlpha = alpha;
      }

      // --- Leading-edge shockwave for freshly reached nodes ---
      if (infected && !isRoot && ignitionAge < 1) {
        ctx.globalAlpha = (1 - ignitionAge) * 0.8;
        ctx.strokeStyle = WAVEFRONT_RING;
        ctx.lineWidth = 1.4 / scale;
        ctx.beginPath();
        ctx.arc(x, y, radius * (1.3 + ignitionAge * 2.4), 0, TAU);
        ctx.stroke();
        ctx.globalAlpha = alpha;
      }

      // --- Wavefront breathing ring: where the infection currently stands ---
      if (onWavefront) {
        const breath = 0.5 + 0.5 * Math.sin((frame.clock / 1000) * 5.2);
        ctx.globalAlpha = (0.3 + breath * 0.5) * alpha;
        ctx.strokeStyle = WAVEFRONT_RING;
        ctx.lineWidth = 1.1 / scale;
        ctx.beginPath();
        ctx.arc(x, y, radius * (1.5 + breath * 0.35), 0, TAU);
        ctx.stroke();
        ctx.globalAlpha = alpha;
      }

      // --- Body ---
      ctx.fillStyle = infected ? INFECTED_BODY : style.body;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, TAU);
      ctx.fill();

      ctx.strokeStyle = infected ? INFECTED_RIM : style.rim;
      ctx.lineWidth = (infected ? 1.3 : 0.9) / scale;
      ctx.stroke();

      // --- Selection ring ---
      if (isSelected || isHovered) {
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = isSelected ? SELECT_RING : SELECT_RING_OUTER;
        ctx.lineWidth = (isSelected ? 2 : 1.3) / scale;
        ctx.beginPath();
        ctx.arc(x, y, radius + 3.2 / scale + 1.4, 0, TAU);
        ctx.stroke();

        if (isSelected) {
          ctx.strokeStyle = SELECT_RING_OUTER;
          ctx.lineWidth = 1 / scale;
          ctx.beginPath();
          ctx.arc(x, y, radius + 6.5 / scale + 2.4, 0, TAU);
          ctx.stroke();
        }
      }

      // --- Label ---
      // Gated on zoom so ~500 nodes do not collapse into unreadable text soup;
      // nodes the user is actively concerned with ignore the gate.
      const forced = isRoot || isSelected || isHovered || node.compromised;
      const show =
        forced ||
        (frame.showLabels && !dimmed) ||
        (frame.showImportantLabels && node.importance > 0.62 && !dimmed);

      if (show) {
        ctx.globalAlpha = dimmed ? DIM_ALPHA : 1;
        ctx.font = frame.labelFont;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        const textY = y + radius + 2.5 / scale;

        // Cheap legibility over a busy canvas: a dark stroke behind the glyphs
        // costs far less than compositing a shadow blur per label.
        ctx.lineWidth = 3 / scale;
        ctx.strokeStyle = TEXT_SHADOW;
        ctx.lineJoin = 'round';
        ctx.strokeText(node.name, x, textY);
        ctx.fillStyle = infected || forced ? TEXT : TEXT_DIM;
        ctx.fillText(node.name, x, textY);
      }

      ctx.globalAlpha = 1;
    },
    // These are read per frame, but they are listed as real dependencies for a
    // second reason: force-graph fires `notifyRedraw` from `onChange` on the
    // `nodeCanvasObject` prop, so a new closure identity is what repaints the
    // canvas after the engine has cooled. Reading them from refs instead would
    // leave a selection or hover invisible until something else forced a frame.
    [infectedIds, relatedIds, hoverId, selectedId, rootId, depthById, wavefrontDepth, isSimulating],
  );

  /** Hit area: the visual radius plus a small forgiving margin. */
  const paintPointerArea = useCallback(
    (node: RadixNode, color: string, ctx: CanvasRenderingContext2D) => {
      const { x, y } = node;
      if (x === undefined || y === undefined) return;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, node.radius + 2.5, 0, TAU);
      ctx.fill();
    },
    [],
  );

  // --- Link styling ------------------------------------------------------

  /**
   * `1` when infection runs source→target along this link, `-1` when it runs
   * target→source, `0` when the link is not on an active infection route.
   * Both endpoints must already be alight, so routes reveal with the wavefront.
   */
  // `infectedIds` is a real dependency here, not an oversight: force-graph
  // rebuilds a link's particles from `onChange` on the `linkDirectionalParticles`
  // prop, which only fires when the accessor's *identity* changes. Reading the
  // infected set from a ref would keep this closure stable, the accessor would
  // never re-run, and the particles would stay frozen at the value computed on
  // hop -1 - that is, none, for the whole simulation. Re-creating a handful of
  // closures once per hop is the cost of the routes lighting up at all.
  const infectionDirection = useCallback(
    (link: RadixLink): number => {
      const from = endpointId(link.source);
      const to = endpointId(link.target);
      if (!infectedIds.has(from) || !infectedIds.has(to)) return 0;
      if (infectionPairs.has(`${from}|${to}`)) return 1;
      if (infectionPairs.has(`${to}|${from}`)) return -1;
      return 0;
    },
    [infectionPairs, infectedIds],
  );

  const linkColor = useCallback(
    (link: RadixLink): string => {
      if (infectionDirection(link) !== 0) return LINK_INFECTED;
      if (relatedIds !== null) {
        const from = endpointId(link.source);
        const to = endpointId(link.target);
        return relatedIds.has(from) && relatedIds.has(to) ? LINK_RELATED : LINK_DIM;
      }
      return link.color;
    },
    [infectionDirection, relatedIds],
  );

  const linkWidth = useCallback(
    (link: RadixLink): number => (infectionDirection(link) !== 0 ? 2.1 : link.width),
    [infectionDirection],
  );

  const linkParticles = useCallback(
    (link: RadixLink): number => (infectionDirection(link) !== 0 ? 3 : 0),
    [infectionDirection],
  );

  /**
   * Negative speed = particles travel target→source.
   *
   * `DEPENDS_ON` points dependent→dependency, but infection propagates
   * dependency→dependent, so most infection routes need the reverse direction.
   */
  const linkParticleSpeed = useCallback(
    (link: RadixLink): number => 0.011 * (infectionDirection(link) === -1 ? -1 : 1),
    [infectionDirection],
  );

  /** Travelling dash laid over the infection routes, on top of the base line. */
  const paintLink = useCallback(
    (link: RadixLink, ctx: CanvasRenderingContext2D, scale: number) => {
      const direction = infectionDirection(link);
      if (direction === 0) return;

      const from = link.source;
      const to = link.target;
      if (typeof from === 'number' || typeof to === 'number') return;
      if (from.x === undefined || from.y === undefined) return;
      if (to.x === undefined || to.y === undefined) return;

      const frame = frameRef.current;
      const dash = 6 / scale;
      const gap = 9 / scale;

      ctx.save();
      ctx.strokeStyle = INFECT_DASH;
      ctx.lineWidth = 1.5 / scale;
      ctx.setLineDash([dash, gap]);
      // 34 graph-units/second of dash travel, flowing the way the infection does.
      ctx.lineDashOffset = ((-frame.clock / 1000) * 34 * direction) / scale;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
      ctx.restore();
    },
    [infectionDirection],
  );

  // --- Interaction -------------------------------------------------------

  const handleNodeClick = useCallback(
    (node: RadixNode) => {
      onSelectNode(typeof node.id === 'number' ? node.id : null);
    },
    [onSelectNode],
  );

  const handleBackgroundClick = useCallback(() => onSelectNode(null), [onSelectNode]);

  const handleNodeHover = useCallback((node: RadixNode | null) => {
    setHoverId(node ? node.id : null);
  }, []);

  // --- Force tuning + framing --------------------------------------------

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || model.nodes.length === 0) return;

    // Spread scales with node count: the default charge leaves a 500-node graph
    // as an unreadable hairball.
    const charge = fg.d3Force('charge') as { strength?: (value: number) => unknown } | undefined;
    charge?.strength?.(model.nodes.length > 260 ? -145 : -95);

    const link = fg.d3Force('link') as { distance?: (value: number) => unknown } | undefined;
    link?.distance?.(38);

    const timer = setTimeout(() => fgRef.current?.zoomToFit(700, 70), 420);
    return () => clearTimeout(timer);
  }, [model]);

  // Pull the viewport onto the breach as it starts, so the propagation is not
  // happening off-screen.
  useEffect(() => {
    if (rootId === null || !isSimulating) return;
    const node = model.byId.get(rootId);
    if (!node) return;
    const timer = setTimeout(() => {
      if (node.x === undefined || node.y === undefined) return;
      fgRef.current?.centerAt(node.x, node.y, 800);
      fgRef.current?.zoom(2.1, 800);
    }, 120);
    return () => clearTimeout(timer);
  }, [rootId, isSimulating, model]);

  // --- Render ------------------------------------------------------------

  const hasData = data !== null && model.nodes.length > 0;
  const ready = size.width > 0 && size.height > 0;

  // The halo and dash animate on their own clock, so the render loop may not
  // idle while anything is alight - `autoPauseRedraw` would freeze mid-pulse.
  const animating = rootId !== null || isSimulating || infectedIds.size > 0;

  return (
    <div
      ref={containerRef}
      className="relative h-full w-full overflow-hidden"
      // Sizing is also inline, not only via the Tailwind classes: the canvas is
      // measured by a ResizeObserver, so if the utility CSS is missing for any
      // reason the container collapses to a few pixels tall and the graph
      // silently renders into a sliver instead of failing loudly.
      style={{ width: '100%', height: '100%', cursor: hoverId !== null ? 'pointer' : 'default' }}
    >
      {hasData && ready ? (
        <ForceGraph2D<RadixNode, RadixLink>
          ref={fgRef}
          width={size.width}
          height={size.height}
          graphData={graphData}
          backgroundColor="rgba(0,0,0,0)"
          nodeRelSize={4}
          nodeVal={(node: RadixNode) => node.radius}
          nodeLabel={(node: RadixNode) => node.tooltip}
          nodeCanvasObject={paintNode}
          nodeCanvasObjectMode={() => 'replace'}
          nodePointerAreaPaint={paintPointerArea}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkCanvasObject={paintLink}
          linkCanvasObjectMode={() => 'after'}
          linkDirectionalParticles={linkParticles}
          linkDirectionalParticleSpeed={linkParticleSpeed}
          linkDirectionalParticleWidth={3}
          linkDirectionalParticleColor={() => INFECT_PARTICLE}
          onRenderFramePre={onRenderFramePre}
          onNodeClick={handleNodeClick}
          onNodeHover={handleNodeHover}
          onBackgroundClick={handleBackgroundClick}
          autoPauseRedraw={!animating}
          cooldownTicks={220}
          d3AlphaDecay={0.024}
          d3VelocityDecay={0.32}
          minZoom={0.25}
          maxZoom={9}
        />
      ) : (
        <GraphPlaceholder empty={data !== null && model.nodes.length === 0} />
      )}

      {/* Chrome only accompanies a live canvas - a legend over the loading
          skeleton would label nothing. Both overlays are pointer-events-none
          throughout, so every wheel/drag/click still lands on the canvas. */}
      {hasData && ready ? (
        <>
          <CanvasLegend showInfectionPath={isSimulating || infectedIds.size > 0} />
          <CanvasHint />
        </>
      ) : null}
    </div>
  );
}

/** Skeleton while the first traversal is in flight, empty state when it returns nothing. */
function GraphPlaceholder({ empty }: { empty: boolean }) {
  return (
    <div className="flex h-full w-full items-center justify-center">
      {empty ? (
        <div className="text-center">
          <div className="label-micro mb-1">dependency graph</div>
          <p className="text-sm text-ink-muted">No nodes returned. Seed the graph to begin.</p>
        </div>
      ) : (
        <div className="flex w-full max-w-md flex-col items-center gap-3 px-8">
          <div className="skeleton h-40 w-40 rounded-full" />
          <div className="skeleton h-2 w-48" />
          <div className="skeleton h-2 w-32" />
          <div className="label-micro mt-1">traversing HydraDB…</div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Canvas chrome
// ---------------------------------------------------------------------------
// Legend and interaction hint, floated over the canvas. Colours come from the
// same `RGB` table the paint callbacks stringify, so the legend can never
// drift from the pixels it describes. Every element here is pointer-events-
// none: the canvas owns all mouse input, and the chrome merely annotates it.

/** One legend row per node kind, in reading order of visual importance. */
const LEGEND_ROWS: ReadonlyArray<{ label: string; colorKey: string }> = [
  { label: 'Package', colorKey: 'cyan' },
  { label: 'Service', colorKey: 'green' },
  { label: 'Maintainer', colorKey: 'violet' },
  { label: 'Version', colorKey: 'slate' },
  { label: 'Lockfile', colorKey: 'amber' },
];

/** Shared shell for every chrome pill: low-opacity glass, hairline, no wrap. */
const CHROME_PILL =
  'flex items-center gap-1.5 whitespace-nowrap rounded-full border border-white/10 ' +
  'bg-deep/55 px-2.5 py-1 backdrop-blur-md';

/**
 * Node-kind legend, top-right.
 *
 * The compromised dot reuses the `.halo-alert` pulse from index.css - the
 * rings inherit the dot's border-radius, so the treatment stays on the dot
 * alone, and the global `prefers-reduced-motion` rule in index.css collapses
 * the animation for users who ask for stillness (the CSS media query covers
 * what usePrefersReducedMotion covers for rAF-driven motion).
 */
function CanvasLegend({ showInfectionPath }: { showInfectionPath: boolean }) {
  return (
    <div
      aria-hidden="true"
      // Hidden below md: on phones the pills collided with the floating
      // console panels. From md up this is exactly the previous rendering.
      className="pointer-events-none absolute right-3 top-3 z-10 hidden select-none flex-col items-end gap-1 md:flex"
    >
      {LEGEND_ROWS.map((row) => (
        <div key={row.label} className={CHROME_PILL}>
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{
              backgroundColor: solid(row.colorKey),
              boxShadow: `0 0 6px ${rgba(row.colorKey, 0.55)}`,
            }}
          />
          <span className="label-micro">{row.label}</span>
        </div>
      ))}

      <div className={CHROME_PILL}>
        <span
          className="halo-alert h-1.5 w-1.5 shrink-0 rounded-full"
          style={{
            backgroundColor: solid('red'),
            boxShadow: `0 0 6px ${rgba('red', 0.55)}`,
          }}
        />
        <span className="label-micro">Compromised</span>
      </div>

      {showInfectionPath ? (
        <div className={CHROME_PILL}>
          {/* Dashed glyph mirroring the travelling dash painted on infected links. */}
          <svg width="14" height="4" viewBox="0 0 14 4" className="shrink-0">
            <line
              x1="1"
              y1="2"
              x2="13"
              y2="2"
              stroke={rgba('red', 0.9)}
              strokeWidth="1.5"
              strokeDasharray="3 2.5"
              strokeLinecap="round"
            />
          </svg>
          <span className="label-micro">Infection path</span>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Interaction hint, bottom-right. One line; the pill sizes to its content.
 *
 * Hidden below sm (it overlapped the panels on phones). One element with two
 * responsive spans: touch wording from sm to lg, pointer wording at lg and up.
 */
function CanvasHint() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute bottom-3 right-3 z-10 hidden select-none sm:block"
    >
      <div className={CHROME_PILL}>
        <span className="text-2xs text-ink-faint lg:hidden">
          pinch to zoom · drag to pan · tap a node
        </span>
        <span className="hidden text-2xs text-ink-faint lg:inline">
          scroll to zoom · drag to pan · click a node to inspect
        </span>
      </div>
    </div>
  );
}

export default GraphCanvas;
