/**
 * Modal presenting the generated remediation PR.
 *
 * The backend returns `diff` as a ready-to-render unified diff string
 * (docs/API_CONTRACT.md), so nothing here reformats it - the parser below only
 * classifies each line and rebuilds the hunk line numbers for the gutter, and
 * the text itself is printed verbatim.
 *
 * Modal semantics are complete: `aria-modal`, a labelled dialog, Escape to
 * dismiss, a focus trap, backdrop click-to-close, and a body scroll lock.
 */

import { useEffect, useId, useMemo, useState } from 'react';

import {
  Badge,
  GlassCard,
  IconButton,
  MicroLabel,
  Spinner,
  cx,
  useBodyScrollLock,
  useCopyToClipboard,
  useEnterTransition,
  useEscapeKey,
  useFocusTrap,
} from './ui';
import type { FixPatch, GenerateFixResponse, OpenPrResponse } from '@/lib/types';

export interface PatchModalProps {
  /** `null` until `POST /api/generate-fix` resolves. */
  patch: GenerateFixResponse | null;
  open: boolean;
  onClose: () => void;
  /**
   * Wire-up for `POST /api/open-pr`. All three props are optional so existing
   * call sites render the modal exactly as before; the OPEN PR affordance and
   * the result strip appear only when the console provides them.
   */
  onOpenPr?: () => void;
  /** True while the open-pr request is in flight. */
  prLoading?: boolean;
  /** The engine's response once it lands; replaces the rendered diff. */
  prResult?: OpenPrResponse | null;
}

// ---------------------------------------------------------------------------
// Unified-diff parsing
// ---------------------------------------------------------------------------

type DiffKind = 'add' | 'del' | 'hunk' | 'meta' | 'context';

interface DiffLine {
  kind: DiffKind;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

function classify(line: string): DiffKind {
  // `+++`/`---` must be tested before `+`/`-`, or file headers colour as edits.
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
  if (line.startsWith('diff ') || line.startsWith('index ')) return 'meta';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'context';
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

function parseDiff(diff: string): DiffLine[] {
  const lines = diff.replace(/\n+$/, '').split('\n');
  let oldNo = 0;
  let newNo = 0;

  return lines.map<DiffLine>((text) => {
    const kind = classify(text);

    if (kind === 'hunk') {
      const match = HUNK_HEADER.exec(text);
      const from = match?.[1];
      const to = match?.[2];
      if (from && to) {
        oldNo = Number(from);
        newNo = Number(to);
      }
      return { kind, text, oldNo: null, newNo: null };
    }

    if (kind === 'meta') return { kind, text, oldNo: null, newNo: null };
    if (kind === 'add') return { kind, text, oldNo: null, newNo: newNo++ };
    if (kind === 'del') return { kind, text, oldNo: oldNo++, newNo: null };
    return { kind, text, oldNo: oldNo++, newNo: newNo++ };
  });
}

const LINE_STYLES: Record<DiffKind, string> = {
  add: 'bg-toxic/10 text-toxic',
  del: 'bg-alert/10 text-alert',
  hunk: 'bg-violet/10 text-violet',
  meta: 'text-ink-faint',
  context: 'text-ink-muted',
};

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  const stats = useMemo(
    () => ({
      added: lines.filter((l) => l.kind === 'add').length,
      removed: lines.filter((l) => l.kind === 'del').length,
    }),
    [lines],
  );

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <Badge accent="green">+{stats.added}</Badge>
        <Badge accent="red">-{stats.removed}</Badge>
        <span className="text-2xs text-ink-faint">unified diff</span>
      </div>

      <div className="overflow-x-auto rounded-xl border border-white/10 bg-black/40">
        <pre className="min-w-max py-2 font-mono text-[11.5px] leading-[1.55]">
          {lines.map((line, index) => (
            <div
              key={`${index}-${line.text}`}
              className={cx('flex items-start px-0', LINE_STYLES[line.kind])}
            >
              <span className="sticky left-0 flex shrink-0 select-none bg-black/40 pr-2 text-ink-faint/70">
                <span className="w-10 px-1 text-right tabular-nums">{line.oldNo ?? ''}</span>
                <span className="w-10 px-1 text-right tabular-nums">{line.newNo ?? ''}</span>
              </span>
              <code className="whitespace-pre px-2">{line.text === '' ? ' ' : line.text}</code>
            </div>
          ))}
        </pre>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// npm overrides block
// ---------------------------------------------------------------------------

const JSON_TOKEN = /("(?:[^"\\]|\\.)*"\s*:)|("(?:[^"\\]|\\.)*")/g;

/** Minimal two-token colouriser - enough for the flat `overrides` object. */
function JsonLine({ text }: { text: string }) {
  const parts: Array<{ text: string; tone: string }> = [];
  let cursor = 0;

  for (const match of text.matchAll(JSON_TOKEN)) {
    const at = match.index;
    if (at === undefined) continue;
    if (at > cursor) parts.push({ text: text.slice(cursor, at), tone: 'text-ink-faint' });
    parts.push({ text: match[0], tone: match[1] ? 'text-cyan' : 'text-amber' });
    cursor = at + match[0].length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), tone: 'text-ink-faint' });

  return (
    <div>
      {parts.map((part, index) => (
        <span key={index} className={part.tone}>
          {part.text}
        </span>
      ))}
    </div>
  );
}

function OverridesView({ overrides }: { overrides: Record<string, string> }) {
  const json = useMemo(() => JSON.stringify({ overrides }, null, 2), [overrides]);
  return (
    <div className="overflow-x-auto rounded-xl border border-white/10 bg-black/40">
      <pre className="min-w-max px-3 py-2.5 font-mono text-[11.5px] leading-[1.6]">
        {json.split('\n').map((line, index) => (
          <JsonLine key={index} text={line} />
        ))}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Copy control
// ---------------------------------------------------------------------------

function CopyButton({
  text,
  label,
  className,
}: {
  text: string;
  label: string;
  className?: string;
}) {
  const [state, copy] = useCopyToClipboard();
  return (
    <button
      type="button"
      onClick={() => copy(text)}
      aria-label={`Copy ${label} to clipboard`}
      className={cx(
        'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-2xs font-medium',
        'transition-all duration-200 ease-swift active:scale-95',
        state === 'copied'
          ? 'border-toxic/45 bg-toxic/10 text-toxic'
          : state === 'failed'
            ? 'border-alert/45 bg-alert/10 text-alert'
            : 'border-white/10 bg-white/[0.04] text-ink-muted hover:border-white/25 hover:text-ink',
        className,
      )}
    >
      {state === 'copied' ? <GlyphCheck /> : <GlyphCopy />}
      {state === 'copied' ? 'COPIED' : state === 'failed' ? 'BLOCKED' : 'COPY'}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Open-PR result strip
// ---------------------------------------------------------------------------

function PrResultStrip({ result }: { result: OpenPrResponse }) {
  const pushed = result.mode === 'pushed';
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-white/10 bg-white/[0.03] px-3.5 py-2.5">
      <Badge accent={pushed ? 'green' : 'amber'}>{pushed ? 'PUSHED' : 'DRY RUN'}</Badge>
      <span className="text-xs text-ink">
        <span className="label-micro mr-2">Branch</span>
        <span className="break-all font-mono text-cyan">{result.branch}</span>
      </span>
      <span className="text-xs text-ink">
        <span className="label-micro mr-2">Base</span>
        <span className="font-mono text-ink-muted">{result.base}</span>
      </span>
      <span className="text-xs text-ink">
        <span className="label-micro mr-2">Regenerated</span>
        <span className={cx('font-mono', result.regenerated ? 'text-toxic' : 'text-amber')}>
          {result.regenerated ? 'yes' : 'no'}
        </span>
      </span>
      {result.pr_url ? (
        <a
          href={result.pr_url}
          target="_blank"
          rel="noreferrer"
          className="break-all text-xs font-medium text-cyan underline decoration-cyan/40 underline-offset-2 transition-colors hover:text-ink"
        >
          {result.pr_url}
        </a>
      ) : null}
      <span className="w-full text-2xs leading-relaxed text-ink-faint">{result.message}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

export function PatchModal({ patch, open, onClose, onOpenPr, prLoading, prResult }: PatchModalProps) {
  const titleId = useId();
  const active = open && patch !== null;

  const dialogRef = useFocusTrap<HTMLDivElement>(active);
  const entered = useEnterTransition(active);
  useEscapeKey(active, onClose);
  useBodyScrollLock(active);

  const [selected, setSelected] = useState(0);
  const patchCount = patch?.patches.length ?? 0;

  // A new fix response invalidates the previously selected service tab.
  useEffect(() => {
    setSelected(0);
  }, [patch]);

  if (!active || !patch) return null;

  const index = Math.min(selected, Math.max(0, patchCount - 1));
  const current: FixPatch | undefined = patch.patches[index];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 max-sm:pb-[max(1rem,env(safe-area-inset-bottom))] sm:p-6"
      role="presentation"
    >
      {/* Backdrop. */}
      <div
        className={cx(
          'absolute inset-0 bg-deep/80 backdrop-blur-sm transition-opacity duration-300',
          entered ? 'opacity-100' : 'opacity-0',
        )}
        onClick={onClose}
        aria-hidden="true"
      />

      <GlassCard
        ref={dialogRef}
        raised
        accent="green"
        glow
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          'relative flex max-h-full w-full max-w-4xl flex-col overflow-hidden outline-none',
          'transition-[transform,opacity] duration-300 ease-swift',
        )}
        style={{
          transform: entered ? 'translate3d(0,0,0) scale(1)' : 'translate3d(0,12px,0) scale(0.985)',
          opacity: entered ? 1 : 0,
        }}
      >
        {/* --- Header ---------------------------------------------------- */}
        <header className="flex items-start gap-3 border-b border-white/10 px-5 py-4">
          <span
            className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-toxic/40 bg-toxic/10 text-toxic"
            aria-hidden="true"
          >
            <GlyphBranch />
          </span>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <MicroLabel>Remediation Pull Request</MicroLabel>
              <Badge accent="green" variant="outline">
                SAFE {patch.safe_version}
              </Badge>
              <Badge accent="slate">
                {patchCount} {patchCount === 1 ? 'service' : 'services'}
              </Badge>
            </div>
            <h2
              id={titleId}
              className="mt-1.5 break-words font-mono text-sm font-semibold leading-snug text-ink"
            >
              {patch.pr_title}
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              <span className="text-toxic">{patch.safe_version}</span> - {patch.reason}
            </p>
          </div>

          <IconButton
            aria-label="Close patch dialog"
            onClick={onClose}
            className="max-sm:h-10 max-sm:w-10"
          >
            <GlyphClose />
          </IconButton>
        </header>

        {/* --- Service selector ------------------------------------------ */}
        {patchCount > 1 ? (
          <div className="flex gap-1.5 overflow-x-auto border-b border-white/10 px-5 py-2.5 scrollbar-none">
            {patch.patches.map((entry, i) => (
              <button
                key={`${entry.service}-${i}`}
                type="button"
                onClick={() => setSelected(i)}
                aria-pressed={i === index}
                className={cx(
                  'shrink-0 rounded-lg border px-3 py-1.5 text-2xs font-medium transition-all duration-200 ease-swift',
                  'max-sm:min-h-[44px]',
                  i === index
                    ? 'border-toxic/50 bg-toxic/10 text-toxic'
                    : 'border-white/10 bg-white/[0.03] text-ink-muted hover:border-white/25 hover:text-ink',
                )}
              >
                {entry.service}
              </button>
            ))}
          </div>
        ) : null}

        {/* --- Body ------------------------------------------------------ */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
          {prResult ? <PrResultStrip result={prResult} /> : null}

          {current ? (
            <>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
                <span className="text-xs text-ink">
                  <span className="label-micro mr-2">Service</span>
                  <span className="font-mono text-toxic">{current.service}</span>
                </span>
                <span className="text-xs text-ink">
                  <span className="label-micro mr-2">Lockfile</span>
                  {/* break-all only bites when the value would overflow, i.e. on
                      narrow phones - wide viewports render identically. */}
                  <span className="break-all font-mono text-amber">{current.lockfile}</span>
                </span>
                <span className="text-xs text-ink">
                  <span className="label-micro mr-2">Commit</span>
                  <span className="stat-numeral break-all font-mono text-ink-muted">
                    {current.commit_hash}
                  </span>
                </span>
              </div>

              <section>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <MicroLabel>
                    {prResult ? 'regenerated lockfile - registry integrity hashes' : 'Patch'}
                  </MicroLabel>
                  <CopyButton
                    text={prResult ? prResult.diff : current.diff}
                    label="the unified diff"
                    className="max-sm:min-h-[44px]"
                  />
                </div>
                {/* The engine's diff is a real commit; it replaces the rendered preview. */}
                <DiffView diff={prResult ? prResult.diff : current.diff} />
              </section>

              <section>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <MicroLabel>package.json - npm overrides</MicroLabel>
                  <CopyButton
                    text={JSON.stringify({ overrides: current.overrides }, null, 2)}
                    label="the npm overrides block"
                    className="max-sm:min-h-[44px]"
                  />
                </div>
                <OverridesView overrides={current.overrides} />
                <p className="mt-2 text-[10px] leading-relaxed text-ink-faint">
                  `overrides` forces every transitive resolution of the compromised package to the
                  safe version, including dependencies that pin it themselves.
                </p>
              </section>
            </>
          ) : (
            <p className="py-8 text-center text-xs text-ink-faint">
              No exposed lockfile required a patch.
            </p>
          )}
        </div>

        {/* --- Footer ---------------------------------------------------- */}
        {/* Below sm the actions stack full-width at a 44px touch height; the
            max-sm:* classes are inert from sm up, so wider viewports keep the
            previous single-row footer exactly. */}
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-white/10 px-5 py-3 max-sm:flex-col max-sm:items-stretch">
          <span className="text-2xs text-ink-faint">
            Generated from the reverse-closure result - no file is written by Radix.
          </span>
          <div className="flex items-center gap-2 max-sm:flex-col max-sm:items-stretch">
            <CopyButton
              text={patch.pr_body}
              label="the PR body"
              className="justify-center max-sm:min-h-[44px]"
            />
            {onOpenPr ? (
              <button
                type="button"
                onClick={onOpenPr}
                disabled={prLoading === true}
                aria-busy={prLoading === true}
                className={cx(
                  'inline-flex items-center gap-1.5 rounded-lg border px-3.5 py-1.5 text-2xs font-semibold',
                  'transition-all duration-200 ease-swift active:scale-95',
                  'border-toxic/50 bg-toxic/10 text-toxic',
                  'hover:border-toxic/70 hover:bg-toxic/15',
                  'justify-center max-sm:min-h-[44px]',
                  prLoading === true && 'cursor-wait opacity-60',
                )}
              >
                {prLoading === true ? (
                  <Spinner size={12} accent="green" label="Opening pull request" />
                ) : null}
                {prLoading === true ? 'OPENING...' : 'OPEN PR (DRY RUN)'}
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className={cx(
                'rounded-lg border border-white/10 bg-white/[0.04] px-3.5 py-1.5 text-2xs',
                'font-medium text-ink-muted transition-all duration-200 ease-swift',
                'hover:border-white/25 hover:bg-white/[0.08] hover:text-ink active:scale-95',
                'max-sm:flex max-sm:min-h-[44px] max-sm:items-center max-sm:justify-center',
              )}
            >
              CLOSE
            </button>
          </div>
        </footer>
      </GlassCard>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Glyphs
// ---------------------------------------------------------------------------

const strokeProps = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.7,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

function GlyphBranch() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
      <circle cx="7" cy="5.5" r="2.5" />
      <circle cx="7" cy="18.5" r="2.5" />
      <circle cx="17" cy="12" r="2.5" />
      <path d="M7 8v8M9.5 12H14.5M9.5 12a5 5 0 0 0 5-5" />
    </svg>
  );
}

function GlyphClose() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

function GlyphCopy() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" {...strokeProps}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
    </svg>
  );
}

function GlyphCheck() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" {...strokeProps}>
      <path d="m5 12.5 4.5 4.5L19 7" />
    </svg>
  );
}

export default PatchModal;
