/**
 * Runs `POST /api/simulate-breach` and then *plays it back* hop by hop.
 *
 * The backend answers in one shot with the complete closure, but dumping 300
 * infected nodes on screen simultaneously reads as a colour change, not as a
 * compromise spreading. So this hook keeps the response whole in `result` and
 * releases `infectedIds` progressively: hop 0 is the compromised root, hop 1 its
 * direct dependents, and so on outward.
 *
 * Hop depth is derived from `closure.paths`, which the API contract guarantees
 * is ordered root-first (they come from `algo.SSpaths` over the materialised
 * `DEPENDED_ON_BY` edge - see docs/HYDRADB_CONTRACT.md §6). A node's depth is the
 * *shortest* route to it, so a package reachable in one hop ignites at hop 1 even
 * if a longer path to it also exists.
 *
 * `paths` is capped server-side (`pathCount`), so the closure can legitimately
 * contain ids that appear on no returned path. Those are not dropped - they are
 * folded in at the final hop, which keeps `infectedIds` a faithful superset of
 * the blast radius once the animation settles.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, isAbortError, simulateBreach } from '../lib/api';
import type { SimulateBreachRequest, SimulateBreachResponse } from '../lib/types';

/** Identify the breached package by id, by name, or with a full request body. */
export type BreachInput = number | string | SimulateBreachRequest;

export interface UseBreachSimulationOptions {
  /** Delay between successive hop reveals. */
  hopIntervalMs?: number;
  /** Beat between the response landing and the root igniting, so the jump reads. */
  ignitionDelayMs?: number;
  /** Fired once the wavefront reaches the outermost hop. */
  onComplete?: (result: SimulateBreachResponse) => void;
}

export interface UseBreachSimulationResult {
  /** Kick off a simulation. Never rejects - failures resolve `null` and set `error`. */
  simulate: (input: BreachInput) => Promise<SimulateBreachResponse | null>;
  /** Clear everything and abort any in-flight request. */
  reset: () => void;
  /**
   * True from the moment `simulate` is called until the wavefront reaches the
   * outermost hop. It gates the *animation* (particles, wavefront pulse) - the
   * infection itself stays lit afterwards, because the settled blast radius is
   * the finding the user came for.
   */
  isSimulating: boolean;
  result: SimulateBreachResponse | null;
  /** Ids infected *so far*. Grows with `currentHop`; a stable Set per hop. */
  infectedIds: Set<number>;
  /** Root-first vertex-id chains driving the edge animation. */
  infectionPaths: number[][];
  /** Hop currently revealed. `-1` before ignition, `0` is the root itself. */
  currentHop: number;
  /** Depth of the outermost hop; `-1` when nothing is loaded. */
  maxHop: number;
  /** Vertex id of the compromised package, for the pulsing halo. */
  rootId: number | null;
  /** True while the HTTP request is in flight (a subset of `isSimulating`). */
  isLoading: boolean;
  error: Error | null;
  /** True once the wavefront has finished and the full radius is displayed. */
  isComplete: boolean;
}

const DEFAULT_HOP_INTERVAL_MS = 460;
const DEFAULT_IGNITION_DELAY_MS = 260;

/** Depth index for one simulation, built once when the response lands. */
interface Propagation {
  depthById: Map<number, number>;
  maxDepth: number;
  paths: number[][];
  rootId: number | null;
}

const EMPTY_PROPAGATION: Propagation = {
  depthById: new Map(),
  maxDepth: -1,
  paths: [],
  rootId: null,
};

const EMPTY_IDS: ReadonlySet<number> = new Set<number>();

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

function normaliseInput(input: BreachInput): SimulateBreachRequest {
  if (typeof input === 'number') return { package_id: input };
  if (typeof input === 'string') return { package_name: input };
  return input;
}

function toError(cause: unknown): Error {
  if (cause instanceof Error) return cause;
  return new Error(typeof cause === 'string' ? cause : 'Breach simulation failed');
}

/**
 * Index every node in the closure by its shortest hop distance from the root.
 *
 * Paths are normally root-inclusive (`[root, …, victim]`). A root-exclusive
 * shape is tolerated by shifting the whole chain out one hop, so a backend that
 * trims the source node cannot silently collapse the animation onto hop 0.
 */
function buildPropagation(result: SimulateBreachResponse | null): Propagation {
  if (!result) return EMPTY_PROPAGATION;

  const rootId = result.root?.id ?? null;
  const paths = result.closure?.paths ?? [];
  const depthById = new Map<number, number>();

  if (rootId !== null) depthById.set(rootId, 0);

  for (const path of paths) {
    if (!path || path.length === 0) continue;
    const rootInclusive = rootId !== null && path[0] === rootId;
    const offset = rootInclusive ? 0 : 1;
    for (let i = 0; i < path.length; i += 1) {
      const id = path[i];
      if (id === undefined) continue;
      const depth = i + offset;
      const known = depthById.get(id);
      if (known === undefined || depth < known) depthById.set(id, depth);
    }
  }

  let maxDepth = depthById.size > 0 ? 0 : -1;
  for (const depth of depthById.values()) {
    if (depth > maxDepth) maxDepth = depth;
  }

  // Closure members that no returned path covers (server-side `pathCount` cap).
  // They belong to the blast radius, so land them on the final wavefront rather
  // than losing them.
  const closure = result.closure;
  const leftovers: number[] = [];
  const collect = (ids: number[] | undefined) => {
    for (const id of ids ?? []) {
      if (!depthById.has(id)) leftovers.push(id);
    }
  };
  collect(closure?.affected_package_ids);
  collect(closure?.affected_service_ids);
  collect(closure?.affected_lockfile_ids);
  for (const node of closure?.affected_nodes ?? []) {
    if (!depthById.has(node.id)) leftovers.push(node.id);
  }

  if (leftovers.length > 0) {
    const tail = Math.max(maxDepth, 1);
    for (const id of leftovers) depthById.set(id, tail);
    maxDepth = tail;
  }

  return { depthById, maxDepth, paths, rootId };
}

export function useBreachSimulation(
  options: UseBreachSimulationOptions = {},
): UseBreachSimulationResult {
  const {
    hopIntervalMs = DEFAULT_HOP_INTERVAL_MS,
    ignitionDelayMs = DEFAULT_IGNITION_DELAY_MS,
    onComplete,
  } = options;

  const [result, setResult] = useState<SimulateBreachResponse | null>(null);
  const [currentHop, setCurrentHop] = useState(-1);
  const [isSimulating, setIsSimulating] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const inFlight = useRef<AbortController | null>(null);
  const ignitionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hopTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const maxDepthRef = useRef(-1);
  const mounted = useRef(true);
  /** Guards against a superseded run resuming the timeline after a newer one starts. */
  const runId = useRef(0);

  // `onComplete` lives in a ref so a caller passing an inline arrow does not
  // have to memoise it to keep the timeline stable.
  const onCompleteRef = useRef(onComplete);
  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  const clearTimers = useCallback(() => {
    if (ignitionTimer.current !== null) {
      clearTimeout(ignitionTimer.current);
      ignitionTimer.current = null;
    }
    if (hopTimer.current !== null) {
      clearInterval(hopTimer.current);
      hopTimer.current = null;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current?.abort();
      inFlight.current = null;
      clearTimers();
    };
  }, [clearTimers]);

  const propagation = useMemo(() => buildPropagation(result), [result]);

  /** Ids at depth ≤ `currentHop`. Recomputed per hop, never per frame. */
  const infectedIds = useMemo(() => {
    if (currentHop < 0 || propagation.depthById.size === 0) return EMPTY_IDS as Set<number>;
    const live = new Set<number>();
    for (const [id, depth] of propagation.depthById) {
      if (depth <= currentHop) live.add(id);
    }
    return live;
  }, [propagation, currentHop]);

  /** Start the wavefront for the run identified by `token`. */
  const startTimeline = useCallback(
    (token: number, response: SimulateBreachResponse, maxDepth: number) => {
      clearTimers();
      maxDepthRef.current = maxDepth;

      const finish = () => {
        setIsSimulating(false);
        onCompleteRef.current?.(response);
      };

      if (maxDepth < 0) {
        finish();
        return;
      }

      // Reduced motion: no travelling wavefront, just the settled result.
      if (prefersReducedMotion() || hopIntervalMs <= 0) {
        setCurrentHop(maxDepth);
        finish();
        return;
      }

      ignitionTimer.current = setTimeout(() => {
        ignitionTimer.current = null;
        if (!mounted.current || runId.current !== token) return;

        setCurrentHop(0);
        if (maxDepth === 0) {
          finish();
          return;
        }

        hopTimer.current = setInterval(() => {
          if (!mounted.current || runId.current !== token) {
            clearTimers();
            return;
          }
          setCurrentHop((previous) => {
            const next = previous + 1;
            if (next >= maxDepthRef.current) {
              clearTimers();
              finish();
              return maxDepthRef.current;
            }
            return next;
          });
        }, hopIntervalMs);
      }, Math.max(0, ignitionDelayMs));
    },
    [clearTimers, hopIntervalMs, ignitionDelayMs],
  );

  const reset = useCallback(() => {
    runId.current += 1;
    inFlight.current?.abort();
    inFlight.current = null;
    clearTimers();
    maxDepthRef.current = -1;
    setResult(null);
    setCurrentHop(-1);
    setIsSimulating(false);
    setIsLoading(false);
    setError(null);
  }, [clearTimers]);

  const simulate = useCallback(
    async (input: BreachInput): Promise<SimulateBreachResponse | null> => {
      const token = (runId.current += 1);

      inFlight.current?.abort();
      clearTimers();
      const controller = new AbortController();
      inFlight.current = controller;

      // Clear the previous radius up front: a new simulation must not appear to
      // start from the last one's infected set.
      setResult(null);
      setCurrentHop(-1);
      setError(null);
      setIsLoading(true);
      setIsSimulating(true);

      try {
        const response = await simulateBreach(normaliseInput(input), {
          signal: controller.signal,
        });
        if (!mounted.current || runId.current !== token) return null;

        setResult(response);
        setIsLoading(false);
        startTimeline(token, response, buildPropagation(response).maxDepth);
        return response;
      } catch (cause) {
        if (isAbortError(cause) || controller.signal.aborted) return null;
        if (!mounted.current || runId.current !== token) return null;
        setError(cause instanceof ApiError ? cause : toError(cause));
        setIsLoading(false);
        setIsSimulating(false);
        return null;
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
      }
    },
    [clearTimers, startTimeline],
  );

  return {
    simulate,
    reset,
    isSimulating,
    result,
    infectedIds,
    infectionPaths: propagation.paths,
    currentHop,
    maxHop: propagation.maxDepth,
    rootId: propagation.rootId,
    isLoading,
    error,
    isComplete: result !== null && !isSimulating && currentHop >= propagation.maxDepth,
  };
}

export default useBreachSimulation;
