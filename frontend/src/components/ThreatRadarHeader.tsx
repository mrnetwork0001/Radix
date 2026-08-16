/**
 * Top bar: the RADIX wordmark, a live radar motif, and the four headline
 * metrics.
 *
 * Every numeral eases to its new value (see `Stat` / `useCountUp`) because the
 * graph mutates in bursts — a breach simulation changes three tiles at once,
 * and easing is what makes that read as a system reacting rather than a
 * re-render.
 */

import { useMemo } from 'react';

import { Badge, GlassCard, Stat, cx, formatNumber } from './ui';
import { navigate } from '@/lib/router';
import type { GraphStats, HealthResponse } from '@/lib/types';

export interface ThreatRadarHeaderProps {
  /** `null` until `GET /api/graph/full` returns; tiles render zeros meanwhile. */
  stats?: GraphStats | null;
  /** Latency of the most recent closure/breach traversal, in float ms. */
  latencyMs?: number | null;
  /** Compromised packages currently flagged. Non-zero turns the tile red. */
  alertCount?: number;
  /** `null` while the first health poll is in flight. */
  health?: HealthResponse | null;
  className?: string;
}

const EMPTY_STATS: GraphStats = {
  total_nodes: 0,
  total_edges: 0,
  packages: 0,
  versions: 0,
  maintainers: 0,
  services: 0,
  lockfiles: 0,
  compromised_packages: 0,
  tracked_lockfiles: 0,
};

// ---------------------------------------------------------------------------
// Radar motif
// ---------------------------------------------------------------------------

/** Fixed blip positions, in the radar's own 100×100 user space. */
const BLIPS: ReadonlyArray<{ x: number; y: number; delay: string }> = [
  { x: 66, y: 36, delay: '0s' },
  { x: 34, y: 62, delay: '0.9s' },
  { x: 58, y: 68, delay: '1.7s' },
];

/**
 * A 60° wedge swept about the dial.
 *
 * `transform-box: view-box` is load-bearing: the default `border-box` would
 * resolve `transform-origin` against the wedge's own bounding box, so the
 * sweep would orbit its own corner instead of the centre of the dial.
 */
function RadarSweep({ size, alerting }: { size: number; alerting: boolean }) {
  const tone = alerting ? 'var(--red-rgb)' : 'var(--cyan-rgb)';
  const spin = { transformBox: 'view-box', transformOrigin: '50px 50px' } as const;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="none"
      className="shrink-0"
      aria-hidden="true"
    >
      <defs>
        <radialGradient id="radix-radar-face" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={`rgb(${tone} / 0.14)`} />
          <stop offset="100%" stopColor={`rgb(${tone} / 0.02)`} />
        </radialGradient>
        <linearGradient id="radix-radar-wedge" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor={`rgb(${tone} / 0.55)`} />
          <stop offset="100%" stopColor={`rgb(${tone} / 0)`} />
        </linearGradient>
      </defs>

      <circle cx="50" cy="50" r="46" fill="url(#radix-radar-face)" />

      {/* Range rings + crosshair. */}
      {[46, 34, 22, 11].map((r) => (
        <circle key={r} cx="50" cy="50" r={r} stroke={`rgb(${tone} / 0.22)`} strokeWidth="0.8" />
      ))}
      <path
        d="M4 50h92M50 4v92"
        stroke={`rgb(${tone} / 0.16)`}
        strokeWidth="0.8"
        strokeDasharray="2 4"
      />

      {/* The sweep itself: wedge + a bright leading edge, rotating together. */}
      <g className="animate-scan-sweep" style={spin}>
        <path d="M50 50 L96 50 A46 46 0 0 0 73 10.2 Z" fill="url(#radix-radar-wedge)" />
        <path d="M50 50 L96 50" stroke={`rgb(${tone} / 0.9)`} strokeWidth="1.1" />
      </g>

      {BLIPS.map((blip) => (
        <circle
          key={`${blip.x}-${blip.y}`}
          cx={blip.x}
          cy={blip.y}
          r="2.2"
          fill={`rgb(${tone} / 1)`}
          className="animate-breathe"
          style={{ animationDelay: blip.delay }}
        />
      ))}

      {/* Contact ring — only while something is actually compromised. */}
      {alerting ? (
        <circle
          cx="50"
          cy="50"
          r="46"
          stroke={`rgb(${tone} / 0.85)`}
          strokeWidth="1.2"
          className="animate-pulse-alert"
          style={spin}
        />
      ) : (
        <circle cx="50" cy="50" r="46" stroke={`rgb(${tone} / 0.35)`} strokeWidth="1" />
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

export function ThreatRadarHeader({
  stats,
  latencyMs,
  alertCount = 0,
  health,
  className,
}: ThreatRadarHeaderProps) {
  const s = stats ?? EMPTY_STATS;
  const alerting = alertCount > 0;
  const latency = typeof latencyMs === 'number' && Number.isFinite(latencyMs) ? latencyMs : 0;

  const link = useMemo(() => {
    if (!health) return { accent: 'slate' as const, text: 'LINKING…', detail: 'contacting backend' };
    if (!health.hydra_ready)
      return { accent: 'red' as const, text: 'HYDRA DOWN', detail: 'degraded — cached view' };
    if (!health.seeded)
      return { accent: 'amber' as const, text: 'UNSEEDED', detail: 'graph is empty' };
    return {
      accent: 'green' as const,
      text: 'HYDRA LIVE',
      detail: `${formatNumber(health.latency_ms, 1)} ms ping`,
    };
  }, [health]);

  return (
    <GlassCard
      as="header"
      accent={alerting ? 'red' : 'cyan'}
      glow
      raised
      className={cx(
        'flex flex-wrap items-center justify-between gap-x-8 gap-y-5 px-5 py-4',
        className,
      )}
      aria-label="Radix threat radar"
    >
      {/* --- Identity --------------------------------------------------- */}
      <div className="flex min-w-0 items-center gap-4">
        {/* The wordmark is the way back to the landing page — SPA-routed so
            the console's state dies with the unmount, exactly like a reload. */}
        <a
          href="/"
          aria-label="Back to the Radix landing page"
          onClick={(event) => {
            event.preventDefault();
            navigate('/');
          }}
          className={cx(
            'group -m-1 flex min-w-0 items-center gap-4 rounded-lg p-1',
            'transition-opacity duration-200 hover:opacity-85 focus-visible:outline-none',
          )}
        >
          <RadarSweep size={62} alerting={alerting} />

          <h1
            className={cx(
              'bg-gradient-to-r from-cyan via-white to-violet bg-clip-text',
              'text-[27px] font-bold leading-none tracking-[0.3em] text-transparent',
            )}
          >
            RADIX
          </h1>
        </a>

        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <Badge accent={link.accent} variant="outline" pulse title={link.detail}>
              {link.text}
            </Badge>
          </div>

          <p className="mt-1.5 truncate text-2xs uppercase tracking-[0.16em] text-ink-faint">
            Supply-chain sentinel
            <span className="mx-1.5 text-white/20">/</span>
            graph-native breach closure
            <span className="mx-1.5 text-white/20">/</span>
            <span className="text-ink-muted">{link.detail}</span>
          </p>
        </div>
      </div>

      {/* --- Live metrics ------------------------------------------------ */}
      <div className="grid min-w-0 flex-1 grid-cols-2 gap-2.5 sm:grid-cols-4 lg:max-w-[46rem]">
        <Stat
          label="Total Nodes"
          value={s.total_nodes}
          accent="cyan"
          hint={`${formatNumber(s.total_edges)} edges`}
          icon={<GlyphNodes />}
        />
        <Stat
          label="Tracked Lockfiles"
          value={s.tracked_lockfiles}
          accent="amber"
          hint={`${formatNumber(s.services)} services watched`}
          icon={<GlyphLock />}
        />

        {/* Announced on change: this tile is the whole point of the header. */}
        <div role="status" aria-live="polite">
          <Stat
            label="Compromise Alerts"
            value={alertCount}
            accent="green"
            alarm={alerting}
            hint={alerting ? 'active supply-chain incident' : 'no active incidents'}
            icon={alerting ? <GlyphAlert /> : <GlyphShield />}
          />
        </div>

        <Stat
          label="Closure Latency"
          value={latency}
          decimals={1}
          suffix="ms"
          accent="cyan"
          hint="HydraDB traversal"
          icon={<GlyphBolt />}
        />
      </div>
    </GlassCard>
  );
}

// ---------------------------------------------------------------------------
// Glyphs — inline so the bundle carries no icon dependency.
// ---------------------------------------------------------------------------

const glyph = {
  width: 14,
  height: 14,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

function GlyphNodes() {
  return (
    <svg {...glyph}>
      <circle cx="5" cy="6" r="2.2" />
      <circle cx="19" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M6.6 7.6 10.6 16M17.4 7.6 13.4 16M7.2 6h9.6" />
    </svg>
  );
}

function GlyphLock() {
  return (
    <svg {...glyph}>
      <rect x="4" y="10" width="16" height="10" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

function GlyphAlert() {
  return (
    <svg {...glyph}>
      <path d="M12 3.6 2.6 20h18.8L12 3.6Z" />
      <path d="M12 9.5v4.5M12 17.2v.1" />
    </svg>
  );
}

function GlyphShield() {
  return (
    <svg {...glyph}>
      <path d="M12 3 4.5 6v6c0 4.4 3.1 7.9 7.5 9 4.4-1.1 7.5-4.6 7.5-9V6L12 3Z" />
      <path d="m9 12 2.2 2.2L15.5 10" />
    </svg>
  );
}

function GlyphBolt() {
  return (
    <svg {...glyph}>
      <path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z" />
    </svg>
  );
}

export default ThreatRadarHeader;
