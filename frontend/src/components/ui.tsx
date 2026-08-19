/**
 * Shared visual primitives for the Radix dashboard.
 *
 * Everything here is a thin wrapper over the design tokens declared in
 * `src/index.css` - this file adds no colours of its own. Tailwind's JIT only
 * sees class names that appear *literally* in source, so the accent palette is
 * a static lookup table of complete class strings rather than something built
 * by string concatenation at render time.
 *
 * Motion rules follow the animation budget stated in index.css: transform and
 * opacity only, and every rAF-driven animation collapses to an instant jump
 * under `prefers-reduced-motion`.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ElementType,
  type HTMLAttributes,
  type ReactNode,
  type RefObject,
} from 'react';

import type { NodeLabel } from '@/lib/types';

// ---------------------------------------------------------------------------
// Class-name helper
// ---------------------------------------------------------------------------

export type ClassValue = string | false | null | undefined;

/** Join truthy class fragments. Deliberately tiny - no `clsx` dependency. */
export function cx(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(' ');
}

// ---------------------------------------------------------------------------
// Accent palette
// ---------------------------------------------------------------------------

export type Accent = 'cyan' | 'green' | 'red' | 'amber' | 'violet' | 'slate';

export interface AccentTokens {
  /** Channel triplet reference for inline `rgb(var(--x-rgb) / a)` colours (SVG fills, gradients). */
  rgb: string;
  /** Solid colour reference, e.g. `var(--cyan)`. */
  color: string;
  text: string;
  border: string;
  fill: string;
  /** `.glow-*` composite from index.css; empty for accents with no glow variant. */
  glow: string;
  textGlow: string;
}

/**
 * Tailwind maps the palette under its own names (`toxic` = green, `alert` =
 * red, `steel` = slate), so this table is also the translation layer between
 * the semantic accent name used in props and the utility classes.
 */
export const ACCENTS: Record<Accent, AccentTokens> = {
  cyan: {
    rgb: 'var(--cyan-rgb)',
    color: 'var(--cyan)',
    text: 'text-cyan',
    border: 'border-cyan/30',
    fill: 'bg-cyan/10',
    glow: 'glow-cyan',
    textGlow: 'glow-text-cyan',
  },
  green: {
    rgb: 'var(--green-rgb)',
    color: 'var(--green)',
    text: 'text-toxic',
    border: 'border-toxic/30',
    fill: 'bg-toxic/10',
    glow: 'glow-green',
    textGlow: 'glow-text-green',
  },
  red: {
    rgb: 'var(--red-rgb)',
    color: 'var(--red)',
    text: 'text-alert',
    border: 'border-alert/40',
    fill: 'bg-alert/10',
    glow: 'glow-red',
    textGlow: 'glow-text-red',
  },
  amber: {
    rgb: 'var(--amber-rgb)',
    color: 'var(--amber)',
    text: 'text-amber',
    border: 'border-amber/30',
    fill: 'bg-amber/10',
    glow: 'glow-amber',
    textGlow: 'glow-text-cyan',
  },
  violet: {
    rgb: 'var(--violet-rgb)',
    color: 'var(--violet)',
    text: 'text-violet',
    border: 'border-violet/30',
    fill: 'bg-violet/10',
    glow: 'glow-violet',
    textGlow: 'glow-text-cyan',
  },
  slate: {
    rgb: 'var(--slate-rgb)',
    color: 'var(--slate)',
    text: 'text-steel',
    border: 'border-steel/25',
    fill: 'bg-steel/10',
    glow: '',
    textGlow: '',
  },
};

/** Node-label → accent, matching the colours the force-graph canvas paints. */
export const LABEL_ACCENT: Record<NodeLabel, Accent> = {
  Package: 'cyan',
  Version: 'slate',
  Maintainer: 'violet',
  Service: 'green',
  Lockfile: 'amber',
};

export function accentForLabel(label: NodeLabel): Accent {
  return LABEL_ACCENT[label];
}

// ---------------------------------------------------------------------------
// Number formatting
// ---------------------------------------------------------------------------

const formatterCache = new Map<number, Intl.NumberFormat>();

function formatterFor(decimals: number): Intl.NumberFormat {
  const cached = formatterCache.get(decimals);
  if (cached) return cached;
  const created = new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  formatterCache.set(decimals, created);
  return created;
}

/** Grouped fixed-precision number, e.g. `1,340` or `24.1`. */
export function formatNumber(value: number, decimals = 0): string {
  if (!Number.isFinite(value)) return '-';
  return formatterFor(decimals).format(value);
}

/** Short magnitude form for large counts: `4.2M`, `18.4k`. */
export function formatCompact(value: number): string {
  if (!Number.isFinite(value)) return '-';
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${formatNumber(value / 1e9, 1)}B`;
  if (abs >= 1e6) return `${formatNumber(value / 1e6, 1)}M`;
  if (abs >= 1e4) return `${formatNumber(value / 1e3, 1)}k`;
  return formatNumber(value, 0);
}

/** `2026-08-11T04:12:00Z` → `11 Aug 2026 · 04:12 UTC`. Returns the input when unparseable. */
export function formatTimestamp(iso: string | undefined): string {
  if (!iso) return '-';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const date = at.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  });
  const time = at.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'UTC',
  });
  return `${date} · ${time} UTC`;
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(query.matches);
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  return reduced;
}

/**
 * Ease a displayed number toward `target` on rAF.
 *
 * The in-flight value lives in a ref, not in the effect closure, so a target
 * that changes mid-animation continues from wherever the counter actually is
 * instead of snapping back to the previous target.
 */
export function useCountUp(target: number, durationMs = 750): number {
  const reduced = usePrefersReducedMotion();
  const currentRef = useRef(target);
  const [display, setDisplay] = useState(target);

  useEffect(() => {
    const from = currentRef.current;
    if (reduced || !Number.isFinite(target) || from === target) {
      currentRef.current = target;
      setDisplay(target);
      return;
    }

    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic
      const value = from + (target - from) * eased;
      currentRef.current = value;
      setDisplay(value);
      if (t < 1) frame = requestAnimationFrame(tick);
    };

    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, durationMs, reduced]);

  return display;
}

/** Fire `onEscape` on the Escape key while `active`. */
export function useEscapeKey(active: boolean, onEscape: () => void): void {
  const handlerRef = useRef(onEscape);
  handlerRef.current = onEscape;

  useEffect(() => {
    if (!active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        handlerRef.current();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [active]);
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Confine Tab/Shift+Tab to the returned element while `active`, move focus in
 * on open, and restore it to the previously focused element on close.
 * For modal dialogs only - a non-modal panel must not trap focus.
 */
export function useFocusTrap<T extends HTMLElement>(active: boolean): RefObject<T> {
  const containerRef = useRef<T>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!active || !container) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusable = (): HTMLElement[] =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );

    (focusable()[0] ?? container).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = focusable();
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        event.preventDefault();
        container.focus();
        return;
      }
      if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      }
    };

    container.addEventListener('keydown', onKeyDown);
    return () => {
      container.removeEventListener('keydown', onKeyDown);
      previouslyFocused?.focus?.();
    };
  }, [active]);

  return containerRef;
}

/** Hold `document.body` still while an overlay owns the viewport. */
export function useBodyScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [active]);
}

/**
 * Enter transition driven by state rather than a keyframe, so it needs no CSS
 * of its own and can be reversed by flipping `active`.
 *
 * The nested `requestAnimationFrame` is deliberate: a single frame can commit
 * the mounted element and its final style in the same paint, which the browser
 * then has no "from" value to transition out of.
 */
export function useEnterTransition(active = true): boolean {
  const [entered, setEntered] = useState(false);

  useEffect(() => {
    if (!active) {
      setEntered(false);
      return;
    }
    let inner = 0;
    const outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(() => setEntered(true));
    });
    return () => {
      cancelAnimationFrame(outer);
      cancelAnimationFrame(inner);
    };
  }, [active]);

  return entered;
}

export type CopyState = 'idle' | 'copied' | 'failed';

/** `navigator.clipboard` with an execCommand fallback for non-secure origins. */
export function useCopyToClipboard(resetAfterMs = 1600): [CopyState, (text: string) => void] {
  const [state, setState] = useState<CopyState>('idle');
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  const copy = useCallback(
    (text: string) => {
      const settle = (next: CopyState) => {
        setState(next);
        if (timerRef.current !== null) window.clearTimeout(timerRef.current);
        timerRef.current = window.setTimeout(() => setState('idle'), resetAfterMs);
      };

      const fallback = () => {
        try {
          const area = document.createElement('textarea');
          area.value = text;
          area.setAttribute('readonly', '');
          area.style.position = 'fixed';
          area.style.opacity = '0';
          document.body.appendChild(area);
          area.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(area);
          settle(ok ? 'copied' : 'failed');
        } catch {
          settle('failed');
        }
      };

      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(() => settle('copied'), fallback);
      } else {
        fallback();
      }
    },
    [resetAfterMs],
  );

  return [state, copy];
}

// ---------------------------------------------------------------------------
// GlassCard
// ---------------------------------------------------------------------------

export interface GlassCardProps extends HTMLAttributes<HTMLElement> {
  /** Semantic tag. Defaults to `div`. */
  as?: ElementType;
  accent?: Accent;
  /** Apply the accent's `.glow-*` composite (border tint + outer bloom). */
  glow?: boolean;
  /** Brighter fill + stronger hairline, for panes that sit above other glass. */
  raised?: boolean;
  /** Diagonal top-left sheen. On by default; turn off for very small chips. */
  sheen?: boolean;
  /** Lift + hairline brighten on hover. */
  interactive?: boolean;
  children?: ReactNode;
}

/** Ref-forwarding so panels can take focus (see NodeInspector). */
export const GlassCard = forwardRef<HTMLElement, GlassCardProps>(function GlassCard(
  {
    as: Tag = 'div',
    accent = 'cyan',
    glow = false,
    raised = false,
    sheen = true,
    interactive = false,
    className,
    children,
    ...rest
  },
  ref,
) {
  return (
    <Tag
      ref={ref}
      className={cx(
        'glass',
        raised && 'glass-raised',
        sheen && 'glass-sheen',
        interactive && 'glass-interactive',
        glow && ACCENTS[accent].glow,
        className,
      )}
      {...rest}
    >
      {children}
    </Tag>
  );
});

// ---------------------------------------------------------------------------
// Stat
// ---------------------------------------------------------------------------

export interface StatProps {
  label: string;
  value: number;
  decimals?: number;
  /** Unit rendered small next to the numeral, e.g. `ms`. */
  suffix?: string;
  hint?: ReactNode;
  accent?: Accent;
  /** Force the red palette and add the pulsing alert halo. */
  alarm?: boolean;
  icon?: ReactNode;
  /** Count up to `value` on change. Off for values that are not really numeric. */
  animate?: boolean;
  className?: string;
}

/**
 * One live metric tile. The numeral eases to its new value so a jump in the
 * graph reads as motion rather than a silent replacement, and `tabular-nums`
 * (via `.stat-numeral`) keeps the digits from shifting horizontally as it ticks.
 */
export function Stat({
  label,
  value,
  decimals = 0,
  suffix,
  hint,
  accent = 'cyan',
  alarm = false,
  icon,
  animate = true,
  className,
}: StatProps) {
  const eased = useCountUp(value);
  const shown = animate ? eased : value;
  const tone = alarm ? ACCENTS.red : ACCENTS[accent];

  return (
    <div
      className={cx(
        'relative rounded-xl border px-3.5 py-2.5 transition-colors duration-300',
        alarm ? 'halo-alert border-alert/45 bg-alert/10' : 'border-white/10 bg-white/[0.03]',
        className,
      )}
      data-stat
    >
      <div className="flex items-center justify-between gap-2">
        <span className="label-micro truncate">{label}</span>
        {icon ? <span className={cx('shrink-0 opacity-80', tone.text)}>{icon}</span> : null}
      </div>

      <div className="mt-1.5 flex items-baseline gap-1">
        <span
          className={cx('stat-numeral text-2xl font-semibold', tone.text, alarm && tone.textGlow)}
        >
          {formatNumber(shown, decimals)}
        </span>
        {suffix ? <span className="text-2xs text-ink-faint">{suffix}</span> : null}
      </div>

      {hint ? <div className="mt-1 truncate text-2xs text-ink-faint">{hint}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

export interface BadgeProps {
  children: ReactNode;
  accent?: Accent;
  variant?: 'soft' | 'outline' | 'solid';
  /** Prefix a breathing status pip. */
  pulse?: boolean;
  title?: string;
  className?: string;
}

export function Badge({
  children,
  accent = 'slate',
  variant = 'soft',
  pulse = false,
  title,
  className,
}: BadgeProps) {
  const tone = ACCENTS[accent];
  const surface =
    variant === 'solid'
      ? cx(tone.fill, tone.border, tone.text, 'border')
      : variant === 'outline'
        ? cx('border bg-transparent', tone.border, tone.text)
        : cx('border border-white/10 bg-white/[0.04]', tone.text);

  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-[3px]',
        'text-2xs font-medium tracking-wide',
        surface,
        className,
      )}
    >
      {pulse ? <span className="pip" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Spinner
// ---------------------------------------------------------------------------

export interface SpinnerProps {
  size?: number;
  accent?: Accent;
  /** Announced to assistive tech; also the tooltip. */
  label?: string;
  className?: string;
}

export function Spinner({ size = 16, accent = 'cyan', label = 'Working', className }: SpinnerProps) {
  const tone = ACCENTS[accent];
  return (
    <svg
      className={cx('animate-spin', className)}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role="status"
      aria-label={label}
    >
      <circle cx="12" cy="12" r="9" stroke={`rgb(${tone.rgb} / 0.2)`} strokeWidth="3" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke={`rgb(${tone.rgb} / 1)`}
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Small parts
// ---------------------------------------------------------------------------

export function Divider({ className }: { className?: string }) {
  return <div className={cx('divider', className)} role="presentation" />;
}

export function MicroLabel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('label-micro', className)}>{children}</div>;
}

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required - icon-only controls have no accessible name otherwise. */
  'aria-label': string;
  children: ReactNode;
}

export function IconButton({ className, children, ...rest }: IconButtonProps) {
  return (
    <button
      type="button"
      className={cx(
        'inline-flex h-8 w-8 items-center justify-center rounded-lg border border-white/10',
        'bg-white/[0.03] text-ink-muted transition-all duration-200 ease-swift',
        'hover:border-white/20 hover:bg-white/[0.08] hover:text-ink active:scale-95',
        'disabled:pointer-events-none disabled:opacity-40',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

/** Label/value row used throughout the inspector and patch views. */
export function MetaRow({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <span className="label-micro shrink-0 pt-[1px]">{label}</span>
      <span
        className={cx(
          'min-w-0 break-words text-right text-xs text-ink',
          mono && 'font-mono text-[11px] tracking-tight',
        )}
      >
        {children}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Brandmark
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
export function RadarSweep({ size }: { size: number }) {
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

export interface BrandmarkProps {
  /** Dial diameter in px; the wordmark scales alongside it. */
  size?: number;
  /**
   * Element for the wordmark text. The console passes `h1` (it is that page's
   * heading); the landing leaves it a span, where the hero owns the h1.
   */
  as?: ElementType;
  className?: string;
}

/**
 * The one Radix identity: swept radar dial plus the gradient wordmark. Shared
 * so the landing header, the landing footer and the console command bar cannot
 * drift into three different logos.
 */
export function Brandmark({ size = 40, as: Text = 'span', className }: BrandmarkProps) {
  return (
    <span className={cx('flex items-center gap-3', className)}>
      <RadarSweep size={size} />
      <Text
        className={cx(
          'bg-gradient-to-r from-cyan via-white to-violet bg-clip-text',
          'font-bold leading-none tracking-[0.3em] text-transparent',
        )}
        style={{ fontSize: Math.round(size * 0.45) }}
      >
        RADIX
      </Text>
    </span>
  );
}
