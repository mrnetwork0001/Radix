/**
 * Radial gauge for the share of services reachable from the compromised
 * package.
 *
 * Pure SVG - no chart library. The 270° value arc is drawn with
 * `pathLength={100}`, which renormalises the path's own length to 100 user
 * units; the dash offset is then literally `100 - percentage`, with no arc-length
 * trigonometry. The sweep is driven by the same eased counter that prints the
 * numeral, so the arc and the digits can never disagree mid-animation.
 *
 * Before any closure exists the gauge shows a true empty state (dimmed track,
 * no numbers) rather than a fake "0%": zero exposure is a real, meaningful
 * result and must stay distinguishable from "nothing computed yet".
 */

import { useMemo } from 'react';

import {
  ACCENTS,
  Badge,
  GlassCard,
  MicroLabel,
  cx,
  formatNumber,
  useCountUp,
  useEnterTransition,
} from './ui';
import type { Accent } from './ui';
import type { BlastRadius } from '@/lib/types';

export interface BlastRadiusGaugeProps {
  /** `null` before any closure has been computed. */
  blastRadius?: BlastRadius | null;
  loading?: boolean;
  /** Rendered diameter in px. */
  size?: number;
  className?: string;
}

// --- Dial geometry, in the SVG's own 200×200 user space ---------------------

const CX = 100;
const CY = 100;
const R = 74;
const START_ANGLE = 135;
const SWEEP = 270;

function polar(radius: number, degrees: number): readonly [number, number] {
  const radians = (degrees * Math.PI) / 180;
  return [CX + radius * Math.cos(radians), CY + radius * Math.sin(radians)] as const;
}

function arcPath(radius: number, fromDeg: number, toDeg: number): string {
  const [x0, y0] = polar(radius, fromDeg);
  const [x1, y1] = polar(radius, toDeg);
  const largeArc = Math.abs(toDeg - fromDeg) > 180 ? 1 : 0;
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${radius} ${radius} 0 ${largeArc} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

const TRACK_PATH = arcPath(R, START_ANGLE, START_ANGLE + SWEEP);
const TICKS = Array.from({ length: 11 }, (_, i) => i * 10);

/** How the reach number is computed - surfaced as a tooltip on the card. */
const METHOD_TOOLTIP =
  'Reverse closure over the materialised DEPENDED_ON_BY edge - every service whose lockfile resolves the compromised package, at any depth.';

interface Severity {
  accent: Accent;
  text: string;
}

function severityFor(percentage: number, tier1Exposed: number): Severity {
  if (percentage <= 0) return { accent: 'green', text: 'CONTAINED' };
  if (tier1Exposed > 0 && percentage >= 25) return { accent: 'red', text: 'CRITICAL' };
  if (percentage >= 60) return { accent: 'red', text: 'CRITICAL' };
  if (percentage >= 25) return { accent: 'red', text: 'SEVERE' };
  if (percentage >= 10) return { accent: 'amber', text: 'ELEVATED' };
  return { accent: 'amber', text: 'LIMITED' };
}

export function BlastRadiusGauge({
  blastRadius,
  loading = false,
  size = 216,
  className,
}: BlastRadiusGaugeProps) {
  const radius = blastRadius ?? null;
  const hasData = radius !== null;

  const percentage = useMemo(() => {
    if (!radius) return 0;
    if (Number.isFinite(radius.percentage)) return Math.min(100, Math.max(0, radius.percentage));
    if (radius.total_services <= 0) return 0;
    return Math.min(100, (radius.exposed_services / radius.total_services) * 100);
  }, [radius]);

  const tier1 = radius?.tier1_exposed ?? 0;
  const severity = severityFor(percentage, tier1);
  const tone = ACCENTS[severity.accent];

  // Hold every counter at zero for the first committed frame, so the arc always
  // sweeps up from empty - even when the gauge only mounts once a closure exists.
  const swept = useEnterTransition();
  const shownPct = useCountUp(swept ? percentage : 0, 1100);
  const shownExposed = useCountUp(swept ? (radius?.exposed_services ?? 0) : 0, 1100);
  const shownTier1 = useCountUp(swept ? tier1 : 0, 1100);

  // The tip marker rides the same eased value as the arc and the numeral.
  const [tipX, tipY] = polar(R, START_ANGLE + (SWEEP * shownPct) / 100);

  return (
    <GlassCard
      as="section"
      accent={hasData ? severity.accent : 'slate'}
      glow={hasData && percentage > 0}
      raised
      title={METHOD_TOOLTIP}
      className={cx('flex flex-col gap-3 p-4', className)}
      aria-label="Blast radius"
    >
      <div className="flex items-center justify-between gap-3">
        <MicroLabel>Blast Radius</MicroLabel>
        <Badge
          accent={loading ? 'cyan' : hasData ? severity.accent : 'slate'}
          variant="outline"
          pulse={loading || (hasData && severity.accent === 'red')}
        >
          {loading ? 'COMPUTING' : hasData ? severity.text : 'STANDBY'}
        </Badge>
      </div>

      {/* --- Dial ---------------------------------------------------------
          Rendered smaller while idle: a full-size empty dial is dead air, and
          the ~28% shrink also gives the card a visible state change the
          moment an incident starts. The transition is width/height, but it
          fires once per incident, not per frame. */}
      <div
        className="relative mx-auto transition-[width,height] duration-300 ease-swift"
        style={{ width: hasData ? size : Math.round(size * 0.72), height: hasData ? size : Math.round(size * 0.72) }}
        {...(hasData
          ? {
              role: 'meter' as const,
              'aria-label': 'Percentage of services exposed',
              'aria-valuemin': 0,
              'aria-valuemax': 100,
              'aria-valuenow': Math.round(percentage),
              'aria-valuetext': `${formatNumber(percentage, 1)} percent of services exposed`,
            }
          : {
              role: 'img' as const,
              'aria-label': 'No active incident. Detonate a breach to compute exposure.',
            })}
      >
        <svg viewBox="0 0 200 200" className="absolute inset-0 h-full w-full" aria-hidden="true">
          <defs>
            <linearGradient id="radix-gauge-arc" gradientUnits="userSpaceOnUse" x1="26" y1="150" x2="174" y2="50">
              <stop offset="0%" stopColor={`rgb(${tone.rgb} / 0.45)`} />
              <stop offset="60%" stopColor={`rgb(${tone.rgb} / 0.95)`} />
              <stop offset="100%" stopColor={`rgb(${tone.rgb} / 1)`} />
            </linearGradient>
            <radialGradient id="radix-gauge-core" cx="50%" cy="50%" r="50%">
              <stop offset="55%" stopColor={`rgb(${tone.rgb} / 0)`} />
              <stop offset="100%" stopColor={`rgb(${tone.rgb} / 0.13)`} />
            </radialGradient>
          </defs>

          {hasData ? <circle cx={CX} cy={CY} r={R - 12} fill="url(#radix-gauge-core)" /> : null}

          {/* Tick ring: lit up to the current value, all dim while no incident. */}
          {TICKS.map((tick) => {
            const angle = START_ANGLE + (SWEEP * tick) / 100;
            const major = tick % 50 === 0;
            const [ix, iy] = polar(R + 9, angle);
            const [ox, oy] = polar(R + (major ? 18 : 14), angle);
            const lit = hasData && tick <= shownPct;
            return (
              <line
                key={tick}
                x1={ix}
                y1={iy}
                x2={ox}
                y2={oy}
                stroke={lit ? `rgb(${tone.rgb} / 0.75)` : 'rgb(255 255 255 / 0.12)'}
                strokeWidth={major ? 1.8 : 1}
                strokeLinecap="round"
              />
            );
          })}

          {/* Track - dimmed further while no incident exists. */}
          <path
            d={TRACK_PATH}
            fill="none"
            stroke={hasData ? 'rgb(255 255 255 / 0.07)' : 'rgb(255 255 255 / 0.045)'}
            strokeWidth="14"
            strokeLinecap="round"
          />

          {hasData ? (
            <>
              {/* Bloom: the same arc, fatter and translucent, standing in for a
                  blur filter at a fraction of the raster cost. */}
              <path
                d={TRACK_PATH}
                fill="none"
                stroke={`rgb(${tone.rgb} / 0.28)`}
                strokeWidth="22"
                strokeLinecap="round"
                pathLength={100}
                strokeDasharray="100"
                strokeDashoffset={100 - shownPct}
              />

              {/* Value arc. */}
              <path
                d={TRACK_PATH}
                fill="none"
                stroke="url(#radix-gauge-arc)"
                strokeWidth="11"
                strokeLinecap="round"
                pathLength={100}
                strokeDasharray="100"
                strokeDashoffset={100 - shownPct}
              />

              {shownPct > 0.5 ? (
                <>
                  <circle cx={tipX} cy={tipY} r="7" fill={`rgb(${tone.rgb} / 0.22)`} />
                  <circle cx={tipX} cy={tipY} r="3.4" fill={tone.color} />
                </>
              ) : null}
            </>
          ) : null}
        </svg>

        {/* Readout, centred over the dial. */}
        {hasData ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <div className="flex items-start">
              <span
                className={cx(
                  'stat-numeral text-[44px] font-bold leading-none',
                  tone.text,
                  percentage > 0 && tone.textGlow,
                )}
              >
                {formatNumber(shownPct, 0)}
              </span>
              <span className={cx('mt-1 ml-0.5 text-lg font-semibold', tone.text)}>%</span>
            </div>
            <span className="label-micro mt-1.5">Services Exposed</span>
            <span className="stat-numeral mt-1 whitespace-nowrap text-sm text-ink-muted">
              {formatNumber(shownExposed, 0)}
              <span className="mx-1 text-ink-faint">/</span>
              {formatNumber(radius?.total_services ?? 0, 0)}
              <span className="ml-1.5 text-2xs text-ink-faint">services</span>
            </span>
          </div>
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center">
            <span className="stat-numeral text-4xl font-semibold leading-none text-ink-faint">
              -
            </span>
            <span className="label-micro mt-2.5">No Active Incident</span>
            <span className="mt-1 text-[10px] leading-4 text-ink-faint">
              detonate a breach to compute exposure
            </span>
          </div>
        )}
      </div>

      {/* --- Called-out detail, only once an incident exists --------------- */}
      {hasData ? (
        <div className="grid grid-cols-2 gap-2">
          <div
            className={cx(
              'rounded-lg border px-3 py-2 transition-colors duration-300',
              tier1 > 0 ? 'halo-alert border-alert/45 bg-alert/10' : 'border-white/10 bg-white/[0.03]',
            )}
          >
            <div className="label-micro whitespace-nowrap">Tier-1</div>
            <div className="mt-0.5 flex items-baseline gap-1.5 whitespace-nowrap">
              <span
                className={cx(
                  'stat-numeral text-xl font-semibold',
                  tier1 > 0 ? 'text-alert glow-text-red' : 'text-toxic',
                )}
              >
                {formatNumber(shownTier1, 0)}
              </span>
              <span className="text-2xs text-ink-faint">exposed</span>
            </div>
          </div>

          <div className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
            <div className="label-micro whitespace-nowrap">Lockfiles</div>
            <div className="mt-0.5 flex items-baseline gap-1 whitespace-nowrap">
              <span className="stat-numeral text-xl font-semibold text-amber">
                {formatNumber(radius?.exposed_lockfiles ?? 0, 0)}
              </span>
              <span className="stat-numeral text-2xs text-ink-faint">
                / {formatNumber(radius?.total_lockfiles ?? 0, 0)}
              </span>
            </div>
          </div>
        </div>
      ) : null}

      {loading ? <div className="skeleton h-1 w-full" aria-hidden="true" /> : null}
    </GlassCard>
  );
}

export default BlastRadiusGauge;
