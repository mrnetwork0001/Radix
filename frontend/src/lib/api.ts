/**
 * Typed fetch wrappers for every endpoint in `docs/API_CONTRACT.md`.
 *
 * All requests are relative to `/api`, which Vite proxies to
 * `http://localhost:8000` in dev (see vite.config.ts) and a reverse proxy
 * serves in production - so no absolute backend URL is ever compiled in.
 *
 * Every call:
 *   - aborts after `REQUEST_TIMEOUT_MS` (a HydraDB traversal that has not
 *     answered by then is a failure, not a slow success),
 *   - honours a caller-supplied `AbortSignal` so a React effect can cancel a
 *     superseded request,
 *   - throws `ApiError` with the server's message on any non-2xx.
 */

import type {
  IngestJob,
  IngestStartRequest,
  OpenPrRequest,
  OpenPrResponse,
  ClosureResponse,
  FullGraphResponse,
  GenerateFixRequest,
  GenerateFixResponse,
  HealthResponse,
  MaintainerRiskResponse,
  SimulateBreachRequest,
  SimulateBreachResponse,
  TyposquatsResponse,
} from './types';

/** Override with `VITE_API_BASE` when the backend is not behind the same origin. */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api';

/** Breach simulation walks the full closure and is the slowest call by design. */
export const REQUEST_TIMEOUT_MS = 30_000;

/** Contract: `?depth=<1-8>` on the closure endpoint. */
export const MIN_DEPTH = 1;
export const MAX_DEPTH = 8;
export const DEFAULT_DEPTH = 6;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  /** Machine-readable code when the backend supplied one. */
  readonly code: string | undefined;
  /** Parsed response body, when it was JSON. */
  readonly body: unknown;

  constructor(
    message: string,
    opts: { status: number; url: string; code?: string; body?: unknown; cause?: unknown },
  ) {
    super(message, { cause: opts.cause });
    this.name = 'ApiError';
    this.status = opts.status;
    this.url = opts.url;
    this.code = opts.code;
    this.body = opts.body;
  }

  /** `0` is reserved for transport failures - the request never reached the backend. */
  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isTimeout(): boolean {
    return this.code === 'timeout';
  }
}

/** `true` when a rejection is the caller's own cancellation and should be swallowed. */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

/**
 * Pull the most useful message out of an error body. FastAPI emits
 * `{"detail": ...}`; HydraDB-shaped errors surface as
 * `{"error": {"code", "message"}}`. Both are handled, plain text as a fallback.
 */
function describeErrorBody(body: unknown, fallback: string): { message: string; code?: string } {
  if (typeof body === 'string' && body.trim()) return { message: body.trim() };
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;

    const error = record.error;
    if (error && typeof error === 'object') {
      const nested = error as Record<string, unknown>;
      if (typeof nested.message === 'string') {
        return {
          message: nested.message,
          ...(typeof nested.code === 'string' ? { code: nested.code } : {}),
        };
      }
    }

    const detail = record.detail;
    if (typeof detail === 'string') return { message: detail };
    // FastAPI validation errors: `detail` is a list of {loc, msg, type}.
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          item && typeof item === 'object' && typeof (item as Record<string, unknown>).msg === 'string'
            ? String((item as Record<string, unknown>).msg)
            : null,
        )
        .filter((m): m is string => m !== null);
      if (messages.length > 0) return { message: messages.join('; ') };
    }

    if (typeof record.message === 'string') return { message: record.message };
  }
  return { message: fallback };
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<T> {
  const url = `${API_BASE}${path}`;
  const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;

  // A local controller lets one abort source cover both the deadline and the
  // caller's signal; `AbortSignal.any` is avoided so this works on older Safari.
  const controller = new AbortController();
  let timedOut = false;

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const external = options.signal;
  const forwardAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener('abort', forwardAbort, { once: true });
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
    });
  } catch (cause) {
    if (timedOut) {
      throw new ApiError(`Request timed out after ${timeoutMs}ms: ${url}`, {
        status: 0,
        url,
        code: 'timeout',
        cause,
      });
    }
    // A caller-initiated abort is not an error condition - re-throw it as-is so
    // effects can filter it with `isAbortError`.
    if (external?.aborted) throw cause;
    throw new ApiError(`Cannot reach the Radix backend at ${url}`, {
      status: 0,
      url,
      code: 'network',
      cause,
    });
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', forwardAbort);
  }

  const body = await readBody(response);

  if (!response.ok) {
    const { message, code } = describeErrorBody(body, `${response.status} ${response.statusText}`);
    throw new ApiError(message, {
      status: response.status,
      url,
      ...(code ? { code } : {}),
      body,
    });
  }

  return body as T;
}

function getJson<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>(path, { method: 'GET' }, options);
}

function postJson<T>(path: string, payload: unknown, options?: RequestOptions): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(payload) }, options);
}

/** Reject non-integer or negative ids here rather than sending a URL the backend will 422 on. */
function assertVertexId(id: number, what: string): void {
  if (!Number.isInteger(id) || id < 0) {
    throw new TypeError(`${what} must be a non-negative integer vertex id, received: ${String(id)}`);
  }
}

function clampDepth(depth: number): number {
  if (!Number.isFinite(depth)) return DEFAULT_DEPTH;
  return Math.min(MAX_DEPTH, Math.max(MIN_DEPTH, Math.round(depth)));
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

/**
 * `GET /api/health`
 *
 * Throws only when the backend itself is unreachable - when HydraDB is down but
 * the API is up, this resolves with `hydra_ready: false`.
 */
export function getHealth(options?: RequestOptions): Promise<HealthResponse> {
  // Health is a liveness probe on a poll loop; a long deadline would let a
  // hung backend stall the status indicator for half a minute.
  return getJson<HealthResponse>('/health', { timeoutMs: 4_000, ...options });
}

/**
 * Health check that never rejects - for the always-on status pill, where an
 * unreachable backend is a state to render, not an exception to handle.
 */
export async function getHealthOrDegraded(options?: RequestOptions): Promise<HealthResponse> {
  try {
    return await getHealth(options);
  } catch (error) {
    if (isAbortError(error)) throw error;
    return { status: 'unreachable', hydra_ready: false, latency_ms: 0, seeded: false };
  }
}

/**
 * `GET /api/graph/full` - the whole dependency graph for the visualiser.
 * `limit` caps returned nodes; omit it for everything.
 */
export function getFullGraph(
  params: { limit?: number } = {},
  options?: RequestOptions,
): Promise<FullGraphResponse> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set('limit', String(Math.max(1, Math.trunc(params.limit))));
  const qs = query.toString();
  return getJson<FullGraphResponse>(`/graph/full${qs ? `?${qs}` : ''}`, options);
}

/**
 * `GET /api/closure/{package_id}` - transitive **reverse** dependency closure.
 *
 * Server-side this is a forward walk over the materialised `DEPENDED_ON_BY`
 * edge, because HydraDB rejects target-anchored variable-length traversal
 * (docs/HYDRADB_CONTRACT.md §3). `depth` maps to that traversal's `*1..N` bound,
 * which the engine requires to be finite - hence the 1–8 clamp.
 */
export function getClosure(
  packageId: number,
  params: { depth?: number } = {},
  options?: RequestOptions,
): Promise<ClosureResponse> {
  assertVertexId(packageId, 'packageId');
  const query = new URLSearchParams();
  if (params.depth !== undefined) query.set('depth', String(clampDepth(params.depth)));
  const qs = query.toString();
  return getJson<ClosureResponse>(`/closure/${packageId}${qs ? `?${qs}` : ''}`, options);
}

/**
 * `POST /api/simulate-breach` - the headline action.
 *
 * Identify the package by `package_id` or `package_name`; supplying neither is
 * rejected here rather than at the backend.
 */
export function simulateBreach(
  payload: SimulateBreachRequest,
  options?: RequestOptions,
): Promise<SimulateBreachResponse> {
  if (payload.package_id === undefined && !payload.package_name) {
    throw new TypeError('simulateBreach requires either package_id or package_name');
  }
  if (payload.package_id !== undefined) assertVertexId(payload.package_id, 'package_id');

  const body: SimulateBreachRequest = {
    ...payload,
    ...(payload.depth !== undefined ? { depth: clampDepth(payload.depth) } : {}),
  };
  return postJson<SimulateBreachResponse>('/simulate-breach', body, options);
}

/**
 * `GET /api/maintainer-risk/{maintainer_id}` - two-hop maintainer subgraph
 * (`Package → MAINTAINED_BY → Maintainer → MAINTAINS → Other_Package`).
 */
export function getMaintainerRisk(
  maintainerId: number,
  options?: RequestOptions,
): Promise<MaintainerRiskResponse> {
  assertVertexId(maintainerId, 'maintainerId');
  return getJson<MaintainerRiskResponse>(`/maintainer-risk/${maintainerId}`, options);
}

/** `GET /api/typosquats/{package_id}` - near-name impostors of a package. */
export function getTyposquats(packageId: number, options?: RequestOptions): Promise<TyposquatsResponse> {
  assertVertexId(packageId, 'packageId');
  return getJson<TyposquatsResponse>(`/typosquats/${packageId}`, options);
}

/**
 * `POST /api/generate-fix` - remediation patches for the exposed services.
 * Omit `service_ids` to patch every exposed service.
 */
export function generateFix(
  payload: GenerateFixRequest,
  options?: RequestOptions,
): Promise<GenerateFixResponse> {
  assertVertexId(payload.package_id, 'package_id');
  payload.service_ids?.forEach((id) => assertVertexId(id, 'service_ids[]'));
  return postJson<GenerateFixResponse>('/generate-fix', payload, options);
}


/**
 * Remediation with a real branch: clone, apply overrides, regenerate the
 * lockfile, and (unless dry_run) push and open the PR. Long-running - the
 * backend clones and runs npm - so the timeout is generous.
 */
export function openPr(payload: OpenPrRequest, options?: RequestOptions): Promise<OpenPrResponse> {
  assertVertexId(payload.package_id, 'package_id');
  assertVertexId(payload.service_id, 'service_id');
  return postJson<OpenPrResponse>('/open-pr', payload, { timeoutMs: 180_000, ...options });
}

/** Kick off ingestion of a repository (local path or git URL). */
export function startIngest(
  payload: IngestStartRequest,
  options?: RequestOptions,
): Promise<{ job_id: string }> {
  return postJson<{ job_id: string }>('/ingest', payload, options);
}

/** Poll one ingestion job. */
export function getIngestJob(jobId: string, options?: RequestOptions): Promise<IngestJob> {
  return getJson<IngestJob>(`/ingest/${encodeURIComponent(jobId)}`, options);
}

export const api = {
  getHealth,
  getHealthOrDegraded,
  getFullGraph,
  getClosure,
  simulateBreach,
  getMaintainerRisk,
  getTyposquats,
  generateFix,
  openPr,
  startIngest,
  getIngestJob,
} as const;
