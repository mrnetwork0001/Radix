/**
 * Radix dashboard composition root.
 *
 * Owns the state the panels disagree about — which node is selected, how deep
 * the traversal runs, whether a patch is on screen — and leaves rendering to
 * the components. The two hooks hold the rest: `useGraphData` for the static
 * ecosystem, `useBreachSimulation` for the incident and its hop-by-hop reveal.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { BlastRadiusGauge } from './components/BlastRadiusGauge';
import { ControlDock } from './components/ControlDock';
import { GraphCanvas } from './components/GraphCanvas';
import { NodeInspector } from './components/NodeInspector';
import { PatchModal } from './components/PatchModal';
import { ThreatRadarHeader } from './components/ThreatRadarHeader';
import { useBreachSimulation } from './hooks/useBreachSimulation';
import { useGraphData } from './hooks/useGraphData';
import { api, DEFAULT_DEPTH, isAbortError } from './lib/api';
import type {
  GenerateFixResponse,
  GraphNode,
  HealthResponse,
  TyposquatCandidate,
} from './lib/types';

/** Demo-world fallback when nothing in the graph is actually flagged. */
const FALLBACK_BREACH_PACKAGE = 'tanstack-query';
const HEALTH_POLL_MS = 15_000;

export default function App() {
  const [depth, setDepth] = useState(DEFAULT_DEPTH);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [patch, setPatch] = useState<GenerateFixResponse | null>(null);
  const [patchOpen, setPatchOpen] = useState(false);
  const [patchLoading, setPatchLoading] = useState(false);
  const [standaloneSquats, setStandaloneSquats] = useState<TyposquatCandidate[] | null>(null);

  const graph = useGraphData();
  const breach = useBreachSimulation();

  // --- Health -------------------------------------------------------------
  // Polled rather than fetched once, so pulling HydraDB out from under a live
  // demo degrades the header instead of freezing it on a stale "ok".
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const next = await api.getHealthOrDegraded();
      if (!cancelled) setHealth(next);
    };
    void poll();
    const timer = window.setInterval(poll, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // --- Derived ------------------------------------------------------------

  const nodesById = useMemo(() => {
    const index = new Map<number, GraphNode>();
    for (const node of graph.data?.nodes ?? []) index.set(node.id, node);
    return index;
  }, [graph.data]);

  const selectedNode = selectedId === null ? null : nodesById.get(selectedId) ?? null;

  /**
   * The breach epicentre: whatever the advisory feed actually flagged, worst
   * first. With real ingested data this is a live finding; the demo world's
   * tanstack-query only appears as a fallback when nothing is flagged.
   */
  const breachTarget = useMemo(() => {
    const flagged = (graph.data?.nodes ?? []).filter(
      (n) => n.label === 'Package' && n.is_compromised,
    );
    if (flagged.length === 0) return null;
    flagged.sort(
      (a, b) =>
        (b.risk_score ?? 0) - (a.risk_score ?? 0) ||
        (b.downloads_weekly ?? 0) - (a.downloads_weekly ?? 0),
    );
    return flagged[0];
  }, [graph.data]);

  const closure = breach.result?.closure ?? null;
  const blastRadius = breach.result?.blast_radius ?? null;

  /** Breach typosquats when an incident is live, otherwise the ad-hoc lookup. */
  const typosquats = breach.result?.typosquats ?? standaloneSquats;

  const alertCount = useMemo(() => {
    if (breach.result) return 1;
    return graph.data?.stats.compromised_packages ?? 0;
  }, [breach.result, graph.data]);

  /** Closure latency is the number worth showing — it is the graph traversal. */
  const latencyMs = closure?.latency_ms ?? graph.latencyMs;

  const status = useMemo(() => {
    if (breach.error) return `simulation failed: ${breach.error.message}`;
    if (breach.isLoading) return 'running reverse closure in HydraDB…';
    if (breach.isSimulating) {
      const hop = Math.max(breach.currentHop, 0);
      return `propagating — hop ${hop} of ${Math.max(breach.maxHop, 0)}`;
    }
    if (breach.result) {
      const { exposed_services: exposed, total_services: total } = breach.result.blast_radius;
      return `closure resolved in ${closure?.latency_ms?.toFixed(1) ?? '—'} ms · ${exposed}/${total} services exposed`;
    }
    if (graph.isInitialLoad) return 'loading ecosystem graph…';
    if (graph.error) return `graph unavailable: ${graph.error.message}`;
    return `${graph.data?.stats.total_nodes ?? 0} nodes tracked · no active incident`;
  }, [breach, closure, graph]);

  // --- Actions ------------------------------------------------------------

  const handleSimulate = useCallback(() => {
    setStandaloneSquats(null);
    void breach.simulate({
      ...(breachTarget
        ? { package_id: breachTarget.id }
        : { package_name: FALLBACK_BREACH_PACKAGE }),
      window_hours: 48,
      depth,
    });
  }, [breach, breachTarget, depth]);

  const handleReset = useCallback(() => {
    breach.reset();
    setSelectedId(null);
    setPatch(null);
    setPatchOpen(false);
    setStandaloneSquats(null);
  }, [breach]);

  const handleGeneratePatch = useCallback(
    async (node: GraphNode) => {
      // The patch always targets the compromised package, even when the click
      // came from a downstream service — that is the thing being pinned.
      const target = closure?.root.id ?? (node.label === 'Package' ? node.id : null);
      if (target === null) return;

      setPatchLoading(true);
      setPatchOpen(true);
      try {
        // bad_version is omitted when no incident is live — the backend
        // resolves the compromised version from the graph itself.
        const result = await api.generateFix({
          package_id: target,
          ...(breach.result?.compromised_version
            ? { bad_version: breach.result.compromised_version }
            : {}),
          ...(node.label === 'Service' ? { service_ids: [node.id] } : {}),
        });
        setPatch(result);
      } catch (error) {
        if (!isAbortError(error)) {
          setPatch(null);
          setPatchOpen(false);
        }
      } finally {
        setPatchLoading(false);
      }
    },
    [breach.result, closure],
  );

  // --- Ad-hoc typosquat lookup -------------------------------------------
  // Only when there is no incident on screen; during a breach the response
  // already carries the epicentre's neighbours.
  const squatRequest = useRef(0);
  useEffect(() => {
    if (breach.result || !selectedNode || selectedNode.label !== 'Package') {
      setStandaloneSquats(null);
      return;
    }
    const ticket = ++squatRequest.current;
    void api
      .getTyposquats(selectedNode.id)
      .then((response) => {
        if (ticket === squatRequest.current) setStandaloneSquats(response.candidates);
      })
      .catch(() => {
        if (ticket === squatRequest.current) setStandaloneSquats(null);
      });
  }, [selectedNode, breach.result]);

  // --- Render -------------------------------------------------------------

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-[color:var(--bg)] text-slate-200">
      <ThreatRadarHeader
        stats={graph.data?.stats ?? null}
        latencyMs={latencyMs}
        alertCount={alertCount}
        health={health}
      />

      <main className="relative flex-1 overflow-hidden">
        <GraphCanvas
          data={graph.data}
          infectedIds={breach.infectedIds}
          infectionPaths={breach.infectionPaths}
          rootId={breach.rootId}
          selectedId={selectedId}
          onSelectNode={setSelectedId}
          isSimulating={breach.isSimulating}
        />

        {/* Controls float over the canvas so the graph keeps the full viewport. */}
        <div className="pointer-events-none absolute inset-y-0 left-0 flex w-[22rem] max-w-[85vw] flex-col justify-end gap-4 p-6">
          <div className="pointer-events-auto">
            <BlastRadiusGauge blastRadius={blastRadius} loading={breach.isLoading} />
          </div>
          <div className="pointer-events-auto">
            <ControlDock
              onSimulate={handleSimulate}
              onReset={handleReset}
              isSimulating={breach.isSimulating}
              depth={depth}
              onDepthChange={setDepth}
              status={status}
              disabled={!graph.data || graph.isInitialLoad}
              targetName={breachTarget?.name ?? FALLBACK_BREACH_PACKAGE}
            />
          </div>
        </div>

        <NodeInspector
          node={selectedNode}
          closure={closure}
          maintainerRisk={breach.result?.maintainer_risk ?? null}
          typosquats={typosquats}
          onClose={() => setSelectedId(null)}
          onGeneratePatch={(node) => void handleGeneratePatch(node)}
          loading={patchLoading}
        />
      </main>

      <PatchModal
        patch={patch}
        open={patchOpen}
        onClose={() => {
          setPatchOpen(false);
          setPatch(null);
        }}
      />
    </div>
  );
}
