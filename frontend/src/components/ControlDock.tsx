/**
 * Action panel: the hero attack simulation, a reset, the traversal-depth
 * control, and a live status line.
 *
 * The depth slider is a real `<input type="range">` laid transparently over a
 * painted track. That keeps native keyboard semantics, the correct ARIA role
 * and value announcements, and pointer/drag behaviour for free, while the
 * visible track can be styled without vendor pseudo-element CSS.
 */

import { useCallback, useId, type ChangeEvent } from 'react';

import { Badge, GlassCard, MicroLabel, Spinner, cx } from './ui';
import { MAX_DEPTH, MIN_DEPTH } from '@/lib/api';

export interface ControlDockProps {
  /** Fires the headline `POST /api/simulate-breach`. */
  onSimulate: () => void;
  /**
   * Package the hero button will breach. With real ingested data the epicentre
   * is whatever the advisory feed actually flagged, so the label cannot be
   * hardcoded to the demo world's tanstack-query.
   */
  targetName?: string | null;
  /** Clears the incident and returns the canvas to the clean graph. */
  onReset: () => void;
  isSimulating?: boolean;
  /** Traversal bound handed to `DEPENDED_ON_BY*1..N`. */
  depth: number;
  onDepthChange: (depth: number) => void;
  /** Single-line human status, e.g. `closure resolved in 6.4 ms`. */
  status?: string;
  /** Disable the hero action when the graph has not loaded yet. */
  disabled?: boolean;
  className?: string;
}

const DEPTHS: readonly number[] = Array.from(
  { length: MAX_DEPTH - MIN_DEPTH + 1 },
  (_, i) => MIN_DEPTH + i,
);

export function ControlDock({
  onSimulate,
  onReset,
  isSimulating = false,
  depth,
  onDepthChange,
  status,
  disabled = false,
  targetName,
  className,
}: ControlDockProps) {
  const sliderId = useId();
  const clamped = Math.min(MAX_DEPTH, Math.max(MIN_DEPTH, Math.round(depth)));
  const fillPct = ((clamped - MIN_DEPTH) / (MAX_DEPTH - MIN_DEPTH)) * 100;

  const handleDepth = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onDepthChange(Number(event.target.value));
    },
    [onDepthChange],
  );

  const busy = isSimulating;

  return (
    <GlassCard
      as="section"
      accent="red"
      raised
      className={cx('flex flex-col gap-4 p-4', className)}
      aria-label="Simulation controls"
    >
      <div className="flex items-center justify-between gap-3">
        <MicroLabel>Incident Console</MicroLabel>
        <Badge accent={busy ? 'red' : 'slate'} pulse={busy}>
          {busy ? 'TRAVERSING' : 'ARMED'}
        </Badge>
      </div>

      {/* --- Hero action -------------------------------------------------- */}
      <button
        type="button"
        onClick={onSimulate}
        disabled={busy || disabled}
        aria-busy={busy}
        aria-label={`Simulate a supply-chain breach of ${targetName ?? 'the compromised package'}`}
        className={cx(
          'group relative w-full overflow-hidden rounded-xl px-4 py-3.5 text-left',
          'border border-alert/50 bg-gradient-to-br from-alert/25 via-alert/10 to-transparent',
          'shadow-glow-red transition-all duration-200 ease-swift',
          'hover:-translate-y-px hover:border-alert/80 hover:from-alert/35',
          'focus-visible:outline-none active:translate-y-0 active:scale-[0.995]',
          'disabled:pointer-events-none disabled:opacity-60',
        )}
      >
        {/* Sheen wipe on hover — transform only, so it composites. */}
        <span
          aria-hidden="true"
          className={cx(
            'pointer-events-none absolute inset-y-0 -left-full w-1/2 skew-x-[-18deg]',
            'bg-gradient-to-r from-transparent via-white/20 to-transparent',
            'transition-transform duration-700 ease-swift group-hover:translate-x-[300%]',
          )}
        />

        <span className="relative flex items-center gap-3">
          <span
            className={cx(
              'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg',
              'border border-alert/50 bg-alert/15 text-alert',
              busy ? '' : 'halo-alert',
            )}
          >
            {busy ? <Spinner accent="red" size={16} label="Simulating breach" /> : <GlyphWorm />}
          </span>

          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold tracking-wide text-alert glow-text-red">
              {busy
                ? 'SIMULATING BREACH…'
                : targetName
                  ? `SIMULATE BREACH: ${targetName.toUpperCase()}`
                  : 'SIMULATE WORM ATTACK'}
            </span>
            <span className="mt-0.5 block truncate text-2xs text-ink-muted">
              inject compromised release → walk the reverse closure
            </span>
          </span>
        </span>
      </button>

      {/* --- Depth ------------------------------------------------------- */}
      <div>
        <div className="mb-2 flex items-baseline justify-between gap-3">
          <label htmlFor={sliderId} className="label-micro cursor-pointer">
            Closure Depth
          </label>
          <span className="stat-numeral text-xs text-ink-muted">
            <span className="font-mono text-cyan">{`*1..${clamped}`}</span>
            <span className="ml-1.5 text-ink-faint">hops</span>
          </span>
        </div>

        <div className="relative h-5">
          {/* Painted track. */}
          <div
            className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 overflow-hidden rounded-full border border-white/10 bg-white/[0.05]"
            aria-hidden="true"
          >
            <div
              className="h-full rounded-full bg-gradient-to-r from-cyan/50 to-cyan transition-[width] duration-200 ease-swift"
              style={{ width: `${fillPct}%` }}
            />
          </div>

          {/* Thumb. */}
          <div
            aria-hidden="true"
            className={cx(
              'pointer-events-none absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full',
              'border border-cyan/70 bg-base shadow-glow-cyan transition-[left] duration-200 ease-swift',
            )}
            style={{ left: `${fillPct}%` }}
          />

          {/* The real control, transparent on top of the painted one. */}
          <input
            id={sliderId}
            type="range"
            min={MIN_DEPTH}
            max={MAX_DEPTH}
            step={1}
            value={clamped}
            onChange={handleDepth}
            disabled={busy}
            aria-label="Transitive closure depth in hops"
            aria-valuetext={`${clamped} hops`}
            className="absolute inset-0 h-full w-full cursor-pointer appearance-none bg-transparent opacity-0 disabled:cursor-not-allowed"
          />
        </div>

        <div className="mt-1 flex justify-between px-[2px]" aria-hidden="true">
          {DEPTHS.map((d) => (
            <span
              key={d}
              className={cx(
                'stat-numeral text-[10px] transition-colors duration-200',
                d === clamped ? 'text-cyan' : d < clamped ? 'text-ink-muted' : 'text-ink-faint/60',
              )}
            >
              {d}
            </span>
          ))}
        </div>

        {/* Why the bound exists at all — HydraDB rejects `*` and `*1..`. */}
        <p className="mt-2 text-[10px] leading-relaxed text-ink-faint">
          HydraDB requires a finite maximum on variable-length traversal, so the closure walk is
          always bounded.
        </p>
      </div>

      {/* --- Reset ------------------------------------------------------- */}
      <button
        type="button"
        onClick={onReset}
        disabled={busy}
        className={cx(
          'inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/10',
          'bg-white/[0.03] px-4 py-2.5 text-xs font-medium tracking-wide text-ink-muted',
          'transition-all duration-200 ease-swift',
          'hover:border-white/25 hover:bg-white/[0.07] hover:text-ink active:scale-[0.99]',
          'disabled:pointer-events-none disabled:opacity-40',
        )}
      >
        <GlyphReset />
        RESET GRAPH
      </button>

      {/* --- Status ------------------------------------------------------ */}
      <div className="rounded-lg border border-white/10 bg-black/25 px-3 py-2">
        <div className="flex items-center gap-2">
          <span
            className={cx('pip shrink-0', busy ? 'text-alert' : status ? 'text-toxic' : 'text-steel')}
            aria-hidden="true"
          />
          <p
            className="min-w-0 flex-1 truncate font-mono text-[11px] leading-5 text-ink-muted"
            role="status"
            aria-live="polite"
          >
            {status ?? 'idle — awaiting operator input'}
          </p>
        </div>
      </div>
    </GlassCard>
  );
}

// ---------------------------------------------------------------------------
// Glyphs
// ---------------------------------------------------------------------------

function GlyphWorm() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 17c2.2 0 2.2-4 4.4-4s2.2 4 4.4 4 2.2-4 4.4-4S18.4 17 21 17" />
      <circle cx="18" cy="7" r="3" />
      <path d="M18 2.4v1.2M18 10.4v1.2M13.4 7h1.2M21.4 7h1.2" />
    </svg>
  );
}

function GlyphReset() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20 11.5A8 8 0 1 1 17.3 6" />
      <path d="M20.5 3.5V9H15" />
    </svg>
  );
}

export default ControlDock;
