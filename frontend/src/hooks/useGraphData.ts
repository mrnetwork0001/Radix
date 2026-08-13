/**
 * Loads the whole dependency graph from `GET /api/graph/full`.
 *
 * The canvas is the only consumer of this data and react-force-graph *mutates*
 * the objects it is handed (it writes `x`/`y`/`vx`/`vy` onto every node during
 * simulation). That makes identity load-bearing: this hook therefore keeps the
 * exact array instances the backend produced and never re-wraps them on a
 * re-render. A refetch deliberately produces brand-new instances — that is the
 * one moment the layout is *supposed* to restart.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, getFullGraph, isAbortError } from '../lib/api';
import type { FullGraphResponse } from '../lib/types';

/**
 * The graph payload as the visualiser consumes it.
 *
 * Structurally identical to `FullGraphResponse`; named separately because the
 * canvas contract is "some payload of nodes + edges", not "whatever this one
 * endpoint happens to return today.
 */
export type GraphPayload = FullGraphResponse;

export interface UseGraphDataOptions {
  /** Cap on returned nodes (`?limit=`). Omit for the full graph. */
  limit?: number;
  /** Set `false` to hold the request back (e.g. until health says `seeded`). */
  enabled?: boolean;
}

export interface UseGraphDataResult {
  data: GraphPayload | null;
  /** True whenever a request is in flight, including a background refetch. */
  loading: boolean;
  error: Error | null;
  /** Re-runs the query. Never rejects — failures land in `error`. */
  refetch: () => Promise<void>;
  /** True only for the first load, when there is nothing to render yet. */
  isInitialLoad: boolean;
  /** Millisecond HydraDB traversal time reported by the backend. */
  latencyMs: number | null;
}

/** Normalise anything thrown by the transport into a real `Error`. */
function toError(cause: unknown): Error {
  if (cause instanceof Error) return cause;
  return new Error(typeof cause === 'string' ? cause : 'Failed to load the dependency graph');
}

export function useGraphData(options: UseGraphDataOptions = {}): UseGraphDataResult {
  const { limit, enabled = true } = options;

  const [data, setData] = useState<GraphPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<Error | null>(null);

  // One controller for the request currently in flight. A refetch (or an
  // unmount) aborts the previous one so a slow response can never overwrite a
  // newer, faster one.
  const inFlight = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, []);

  const load = useCallback(async (): Promise<void> => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setLoading(true);
    setError(null);

    try {
      const payload = await getFullGraph(limit === undefined ? {} : { limit }, {
        signal: controller.signal,
      });
      if (controller.signal.aborted || !mounted.current) return;
      setData(payload);
    } catch (cause) {
      // A supersede/unmount abort is our own doing, not a failure to report.
      if (isAbortError(cause) || controller.signal.aborted) return;
      if (!mounted.current) return;
      setError(
        cause instanceof ApiError
          ? cause
          : toError(cause),
      );
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    void load();
    // `load` aborts its own previous request; no extra cleanup needed here.
  }, [enabled, load]);

  return {
    data,
    loading,
    error,
    refetch: load,
    isInitialLoad: loading && data === null,
    latencyMs: data?.latency_ms ?? null,
  };
}

export default useGraphData;
