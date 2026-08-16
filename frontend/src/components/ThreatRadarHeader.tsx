/**
 * Command bar: RADIX wordmark + radar motif on the left, link health beside it,
 * four headline metric tiles on the right. One row at lg+, ~64-72px tall.
 *
 * Below lg the bar restacks into two slim rows: identity + link health with an
 * optional hamburger at the right edge, then the four tiles as a horizontal
 * snap-scroll strip (one finger-swipe tall, never wrapping). Every mobile
 * difference lives behind responsive variants; at lg+ the classes resolve to
 * exactly the original single-row layout.
 *
 * Discipline rules this component enforces:
 * - The bar itself is neutral glass at all times. The red treatment (border
 *   tint + pulse) is confined to the ALERTS tile, and only while
 *   `alertCount > 0`.
 * - Nothing here may ellipsize. Every tile pairs `whitespace-nowrap` with a
 *   min-width sized to its longest realistic content (math at MIN_W below),
 *   so tiles grow rather than clip if a value outruns the estimate.
 * - Numerals ease to new values via `useCountUp` because breach simulations
 *   change several tiles at once; easing reads as a system reacting.
 */

import { useMemo } from 'react';

import { ACCENTS, Badge, GlassCard, cx, formatNumber, useCountUp, type Accent } from './ui';
import { navigate } from '@/lib/router';
import type { GraphStats, HealthResponse } from '@/lib/types';

export interface ThreatRadarHeaderProps {
  /** `null` until `GET /api/graph/full` returns; tiles render zeros meanwhile. */
  stats?: GraphStats | null;
  /**
   * Latency of the most recent closure/breach traversal, in float ms.
   * `null`/`undefined` means no closure has run yet - the CLOSURE tile renders
   * a dimmed "-" with an "awaiting incident" caption.
   */
  latencyMs?: number | null;
  /** Compromised packages currently flagged. Non-zero turns the ALERTS tile red. */
  alertCount?: number;
  /** `null` while the first health poll is in flight. */
  health?: HealthResponse | null;
  /**
   * Optional: an incident workflow is open somewhere in the console. Adds a
   * restrained red hairline to the ALERTS tile - never anything page-wide.
   */
  incidentActive?: boolean;
  /**
   * Optional: whether the mobile mission-controls drawer is open. Only read
   * when `onToggleMenu` is provided; drives the hamburger's X morph and its
   * `aria-expanded`.
   */
  menuOpen?: boolean;
  /**
   * Optional: when provided, a hamburger button renders at the right edge of
   * the identity row - below lg only. The orchestrator owns the drawer; this
   * component only reports the toggle.
   */
  onToggleMenu?: () => void;
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

/** Fixed blip positions, in the radar's own 100x100 user space. */
const BLIPS: ReadonlyArray<{ x: number; y: number; delay: string }> = [
  { x: 66, y: 36, delay: '0s' },
  { x: 34, y: 62, delay: '0.9s' },
  { x: 58, y: 68, delay: '1.7s' },
];

/**
 * A 60 degree wedge swept about the dial. Always cyan: the radar is identity
 * chrome, not an alarm surface (alert state lives in the ALERTS tile alone).
 *
 * `transform-box: view-box` is load-bearing: the default `border-box` would
 * resolve `transform-origin` against the wedge's own bounding box, so the
 * sweep would orbit its own corner instead of the centre of the dial.
 */
function RadarSweep({ size }: { size: number }) {
  const tone = 'var(--cyan-rgb)';
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

      <circle cx="50" cy="50" r="46" stroke={`rgb(${tone} / 0.35)`} strokeWidth="1" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Mobile menu toggle
// ---------------------------------------------------------------------------

/**
 * Shared geometry for the three hamburger bars: 16px wide, 2px tall, vertically
 * centred in a 16px box. The rest state fans them out +/-5px; the open state
 * collapses the outer pair onto the centre and rotates them into an X while the
 * middle bar fades. Transform + opacity only, so the morph runs on the
 * compositor and honours the global reduced-motion collapse.
 */
const MENU_BAR =
  'absolute left-0 top-[7px] block h-[2px] w-4 rounded-full bg-current ' +
  'transition-[transform,opacity] duration-200 ease-swift';

/**
 * 40x40 glass square, visible only below lg. The orchestrator wires
 * `onToggleMenu`/`menuOpen`; without the callback the button never mounts, so
 * the desktop DOM is untouched even before the orchestrator catches up.
 */
function MenuToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label="Toggle mission controls"
      aria-expanded={open}
      className={cx(
        'ml-auto inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg lg:hidden',
        'border border-white/10 bg-white/[0.03] text-ink-muted',
        'transition-colors duration-200 ease-swift',
        'hover:border-white/20 hover:bg-white/[0.08] hover:text-ink active:scale-95',
      )}
    >
      <span className="relative block h-4 w-4" aria-hidden="true">
        <span className={cx(MENU_BAR, open ? 'rotate-45' : '-translate-y-[5px]')} />
        <span className={cx(MENU_BAR, open ? 'opacity-0' : 'opacity-100')} />
        <span className={cx(MENU_BAR, open ? '-rotate-45' : 'translate-y-[5px]')} />
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Metric tile
// ---------------------------------------------------------------------------

/**
 * Min-width proof at the tile's type scale (label-micro 11px caps at +0.11em
 * tracking ~7.5px/glyph; text-xl tabular numerals ~11px/glyph; text-2xs
 * captions 11px at +0.04em ~6px/glyph; plus px-3 = 24px inner padding):
 *
 *   NODES     label "NODES" 38px · numeral "99,999" 66px · caption
 *             "99,999 edges" 72px  -> 96px  content, min-w 6.5rem (104px)
 *   LOCKFILES label "LOCKFILES" 68px · caption "99 services" 66px
 *                                  -> 92px  content, min-w 6.5rem (104px)
 *   ALERTS    caption "active incident" 90px
 *                                  -> 114px content, min-w 7.25rem (116px)
 *   CLOSURE   numeral "1,234.5" 77px · caption "awaiting incident" 102px
 *                                  -> 126px content, min-w 8rem (128px)
 *
 * Every line is `whitespace-nowrap` and nothing sets `overflow: hidden` or
 * `truncate`, so content beyond these estimates widens the tile instead of
 * clipping.
 */
function MetricTile({
  label,
  value,
  decimals = 0,
  caption,
  accent = 'cyan',
  alarm = false,
  hairline = false,
  className,
}: {
  label: string;
  /** `null` renders a dimmed "-" (metric not applicable yet). */
  value: number | null;
  decimals?: number;
  caption: string;
  accent?: Accent;
  /** Red border tint + pulse. ALERTS tile only, and only while alerts exist. */
  alarm?: boolean;
  /** Restrained red hairline without the pulse (incident open, zero alerts). */
  hairline?: boolean;
  className?: string;
}) {
  const eased = useCountUp(value ?? 0);
  const tone = alarm ? ACCENTS.red : ACCENTS[accent];
  const empty = value === null;

  return (
    <div
      className={cx(
        'whitespace-nowrap rounded-lg border px-3 py-1 transition-colors duration-300',
        alarm
          ? 'halo-alert border-alert/45 bg-alert/10'
          : hairline
            ? 'border-alert/40 bg-white/[0.03]'
            : 'border-white/10 bg-white/[0.03]',
        className,
      )}
      data-stat
    >
      <div className="label-micro leading-none">{label}</div>
      <div
        className={cx(
          'stat-numeral mt-0.5 text-xl font-semibold leading-none',
          empty ? 'text-ink-faint' : tone.text,
          alarm && tone.textGlow,
        )}
      >
        {empty ? '-' : formatNumber(eased, decimals)}
      </div>
      <div className="mt-0.5 text-2xs leading-none text-ink-faint">{caption}</div>
    </div>
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
  incidentActive = false,
  menuOpen = false,
  onToggleMenu,
  className,
}: ThreatRadarHeaderProps) {
  const s = stats ?? EMPTY_STATS;
  const alerting = alertCount > 0;
  const closureMs =
    typeof latencyMs === 'number' && Number.isFinite(latencyMs) ? latencyMs : null;
  const pingMs =
    typeof health?.latency_ms === 'number' && Number.isFinite(health.latency_ms)
      ? health.latency_ms
      : null;

  const link = useMemo(() => {
    if (!health) return { accent: 'slate' as const, text: 'LINKING', detail: 'contacting backend' };
    if (!health.hydra_ready)
      return { accent: 'red' as const, text: 'DEGRADED', detail: 'HydraDB unreachable - cached view' };
    if (!health.seeded)
      return { accent: 'amber' as const, text: 'UNSEEDED', detail: 'graph is empty' };
    return { accent: 'green' as const, text: 'HYDRA LIVE', detail: 'backend link healthy' };
  }, [health]);

  return (
    <GlassCard
      as="header"
      accent="cyan"
      glow
      raised
      className={cx(
        // Mobile: two stacked slim rows. lg+: the original single row, with
        // every base class overridden back to its historical value.
        'flex flex-col gap-y-2 px-3 py-2',
        'lg:flex-row lg:flex-wrap lg:items-center lg:justify-between lg:gap-x-6 lg:gap-y-3 lg:px-4 lg:py-1.5',
        className,
      )}
      aria-label="Radix threat radar"
    >
      {/* --- Identity + link health -------------------------------------- */}
      <div className="flex items-center gap-3 lg:gap-4">
        {/* The wordmark is the way back to the landing page - SPA-routed so
            the console's state dies with the unmount, exactly like a reload. */}
        <a
          href="/"
          aria-label="Back to the Radix landing page"
          onClick={(event) => {
            event.preventDefault();
            navigate('/');
          }}
          className={cx(
            'group -m-1 flex items-center gap-3 rounded-lg p-1',
            'transition-opacity duration-200 hover:opacity-85 focus-visible:outline-none',
          )}
        >
          <RadarSweep size={40} />

          <h1
            className={cx(
              'bg-gradient-to-r from-cyan via-white to-violet bg-clip-text',
              'text-lg font-bold leading-none tracking-[0.3em] text-transparent',
            )}
          >
            RADIX
          </h1>
        </a>

        <div className="flex items-center gap-2">
          <Badge accent={link.accent} variant="outline" pulse title={link.detail}>
            {link.text}
          </Badge>

          {pingMs !== null ? (
            <span className="chip tabular" title="Health-poll round trip">
              {formatNumber(pingMs, 0)} ms
            </span>
          ) : null}
        </div>

        {onToggleMenu ? <MenuToggle open={menuOpen} onToggle={onToggleMenu} /> : null}
      </div>

      {/* --- Live metrics ------------------------------------------------
          Mobile: a horizontal snap-scroll strip, one finger-swipe tall. The
          negative margin lets tiles scroll edge-to-edge under the card's
          padding, and scroll-px keeps snap stops aligned with that padding.
          lg+: the original inline wrapping row. */}
      <div
        className={cx(
          '-mx-3 flex snap-x snap-mandatory items-stretch gap-2 overflow-x-auto',
          'scroll-px-3 whitespace-nowrap px-3 scrollbar-none',
          'lg:mx-0 lg:flex-wrap lg:snap-none lg:overflow-x-visible lg:scroll-px-0 lg:whitespace-normal lg:px-0',
        )}
      >
        <MetricTile
          label="NODES"
          value={s.total_nodes}
          accent="cyan"
          caption={`${formatNumber(s.total_edges)} edges`}
          className="min-w-[6.5rem] shrink-0 snap-start"
        />
        <MetricTile
          label="LOCKFILES"
          value={s.tracked_lockfiles}
          accent="amber"
          caption={`${formatNumber(s.services)} services`}
          className="min-w-[6.5rem] shrink-0 snap-start"
        />

        {/* Announced on change: this tile is the whole point of the header. */}
        <div role="status" aria-live="polite" className="flex shrink-0 snap-start items-stretch">
          <MetricTile
            label="ALERTS"
            value={alertCount}
            accent="green"
            alarm={alerting}
            hairline={incidentActive}
            // "active incident" only while one is actually running; a flagged
            // package at rest is a finding, not an incident - the CLOSURE tile
            // next door says "awaiting incident", and the two must not conflict.
            caption={
              incidentActive
                ? 'active incident'
                : alerting
                  ? alertCount === 1
                    ? 'flagged package'
                    : 'flagged packages'
                  : 'supply-chain'
            }
            className="min-w-[7.25rem]"
          />
        </div>

        <MetricTile
          label="CLOSURE"
          value={closureMs}
          decimals={1}
          accent="cyan"
          caption={closureMs === null ? 'awaiting incident' : 'ms · HydraDB'}
          className="min-w-[8rem] shrink-0 snap-start"
        />
      </div>
    </GlassCard>
  );
}

export default ThreatRadarHeader;
