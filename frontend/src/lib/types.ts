/**
 * TypeScript mirror of `docs/API_CONTRACT.md`.
 *
 * This file is a transcription, not a design — field names, casing and optional
 * markers follow the frozen contract exactly. If something here disagrees with
 * that document, the document wins.
 *
 * Two contract details drive most of the optionality below:
 *
 *  1. `GraphNode` is a union flattened into one object. The backend omits fields
 *     that do not apply to a node's `label`, so consumers must branch on `label`
 *     rather than probing for a field. The `*Node` aliases and `is*` guards at
 *     the bottom of this file do that narrowing once.
 *  2. `DEPENDED_ON_BY` and `MAINTAINS` are HydraDB's materialised inverse edges
 *     (see docs/HYDRADB_CONTRACT.md §3). They exist only to make reverse closure
 *     a legal forward traversal and are never serialised, so `EdgeType`
 *     deliberately excludes them.
 */

// ---------------------------------------------------------------------------
// Core object shapes
// ---------------------------------------------------------------------------

export type NodeLabel = 'Package' | 'Version' | 'Maintainer' | 'Service' | 'Lockfile';

export type EdgeType = 'DEPENDS_ON' | 'MAINTAINED_BY' | 'RESOLVED_IN' | 'TYPOSQUAT_OF';

/** ISO-8601 UTC timestamp, e.g. `"2026-08-11T04:12:00Z"`. */
export type IsoTimestamp = string;

export interface GraphNode {
  /** HydraDB integer vertex id. Its magnitude also encodes the label — see backend/app/schema.py. */
  id: number;
  label: NodeLabel;
  /** Display name; present on every node type. */
  name: string;

  // --- Package / Version ---
  ecosystem?: string;

  // --- Package only ---
  is_compromised?: boolean;
  /** 0.0–1.0 */
  risk_score?: number;
  downloads_weekly?: number;

  // --- Version only ---
  semver?: string;
  published_at?: IsoTimestamp;
  compromised_window?: boolean;

  // --- Maintainer only ---
  username?: string;
  email?: string;
  key_fingerprint?: string;

  // --- Service only ---
  repo_url?: string;
  criticality?: string;

  // --- Lockfile only ---
  filename?: string;
  commit_hash?: string;
}

export interface GraphEdge {
  /** Source vertex id. */
  source: number;
  /** Target vertex id. */
  target: number;
  type: EdgeType;

  // --- DEPENDS_ON only ---
  constraint?: string;
  is_dev?: boolean;
  transitive_depth?: number;

  // --- MAINTAINED_BY only ---
  since?: IsoTimestamp;

  // --- RESOLVED_IN only ---
  resolved_version?: string;

  // --- TYPOSQUAT_OF only ---
  edit_distance?: number;
  similarity_score?: number;
}

export interface BlastRadius {
  exposed_services: number;
  total_services: number;
  /** Percentage of services exposed, 0–100. */
  percentage: number;
  exposed_lockfiles: number;
  total_lockfiles: number;
  tier1_exposed: number;
}

/** A `GraphNode` carrying the extra fields the maintainer-risk view attaches. */
export type SisterPackage = GraphNode & {
  flagged: boolean;
  published_within_window: boolean;
};

/** A `GraphNode` carrying the extra fields the typosquat view attaches. */
export type TyposquatCandidate = GraphNode & {
  edit_distance: number;
  similarity_score: number;
};

/**
 * Timeline severities the UI styles explicitly. The `(string & {})` arm keeps
 * an unrecognised level from breaking the build while preserving autocomplete
 * on the known ones.
 */
export type Severity = 'critical' | 'high' | 'warning' | 'info' | (string & {});

export interface TimelineEvent {
  /** ISO-8601 instant the event occurred. */
  t: IsoTimestamp;
  event: string;
  severity: Severity;
}

// ---------------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  /** `false` (rather than an error) when HydraDB is unreachable, so the dashboard can degrade. */
  hydra_ready: boolean;
  latency_ms: number;
  seeded: boolean;
}

// ---------------------------------------------------------------------------
// GET /api/graph/full
// ---------------------------------------------------------------------------

export interface GraphStats {
  total_nodes: number;
  total_edges: number;
  packages: number;
  versions: number;
  maintainers: number;
  services: number;
  lockfiles: number;
  compromised_packages: number;
  tracked_lockfiles: number;
}

export interface FullGraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: GraphStats;
  latency_ms: number;
}

// ---------------------------------------------------------------------------
// GET /api/closure/{package_id}
// ---------------------------------------------------------------------------

export interface ClosureResponse {
  root: GraphNode;
  depth: number;
  latency_ms: number;
  affected_package_ids: number[];
  affected_service_ids: number[];
  affected_lockfile_ids: number[];
  /** Hydrated nodes for every id above. */
  affected_nodes: GraphNode[];
  /**
   * Vertex-id chains from `algo.SSpaths`, ordered root-first outward to the
   * victim. Drives the infection animation, so order is load-bearing.
   */
  paths: number[][];
  blast_radius: BlastRadius;
}

// ---------------------------------------------------------------------------
// GET /api/maintainer-risk/{maintainer_id}
// ---------------------------------------------------------------------------

/** Shared body; embedded verbatim inside `SimulateBreachResponse` without `latency_ms`. */
export interface MaintainerRisk {
  maintainer: GraphNode;
  sister_packages: SisterPackage[];
  risk_note: string;
}

export type MaintainerRiskResponse = MaintainerRisk & { latency_ms: number };

// ---------------------------------------------------------------------------
// GET /api/typosquats/{package_id}
// ---------------------------------------------------------------------------

export interface TyposquatsResponse {
  target: GraphNode;
  candidates: TyposquatCandidate[];
  latency_ms: number;
}

// ---------------------------------------------------------------------------
// POST /api/simulate-breach
// ---------------------------------------------------------------------------

/** Supply `package_id` or `package_name` — the backend accepts either. */
export interface SimulateBreachRequest {
  package_id?: number;
  package_name?: string;
  version?: string;
  window_hours?: number;
  depth?: number;
}

export interface SimulateBreachResponse {
  incident_id: string;
  root: GraphNode;
  compromised_version: string;
  closure: ClosureResponse;
  maintainer_risk: MaintainerRisk;
  typosquats: TyposquatCandidate[];
  blast_radius: BlastRadius;
  timeline: TimelineEvent[];
  latency_ms: number;
}

// ---------------------------------------------------------------------------
// POST /api/generate-fix
// ---------------------------------------------------------------------------

export interface GenerateFixRequest {
  package_id: number;
  /** Omit to let the backend pick the earliest release inside the window. */
  bad_version?: string;
  /** Omit to patch every exposed service. */
  service_ids?: number[];
}

export interface FixPatch {
  service: string;
  lockfile: string;
  commit_hash: string;
  /** Ready-to-render unified diff; the UI prints it verbatim with +/- colouring. */
  diff: string;
  /** Package name → pinned safe version. */
  overrides: Record<string, string>;
}

export interface GenerateFixResponse {
  safe_version: string;
  reason: string;
  patches: FixPatch[];
  pr_title: string;
  /** Markdown. */
  pr_body: string;
}

// ---------------------------------------------------------------------------
// Label-narrowed views + guards
// ---------------------------------------------------------------------------

export type PackageNode = GraphNode & { label: 'Package' };
export type VersionNode = GraphNode & { label: 'Version' };
export type MaintainerNode = GraphNode & { label: 'Maintainer' };
export type ServiceNode = GraphNode & { label: 'Service' };
export type LockfileNode = GraphNode & { label: 'Lockfile' };

export const isPackageNode = (n: GraphNode): n is PackageNode => n.label === 'Package';
export const isVersionNode = (n: GraphNode): n is VersionNode => n.label === 'Version';
export const isMaintainerNode = (n: GraphNode): n is MaintainerNode => n.label === 'Maintainer';
export const isServiceNode = (n: GraphNode): n is ServiceNode => n.label === 'Service';
export const isLockfileNode = (n: GraphNode): n is LockfileNode => n.label === 'Lockfile';

/** True only for a Package the backend has flagged. */
export const isCompromised = (n: GraphNode): boolean => n.is_compromised === true;
