/**
 * Repository submission from the console: a compact "ADD REPO" rail button
 * that opens a modal for kicking off a real ingestion job.
 *
 * Flow: `POST /api/ingest` starts the job, then `GET /api/ingest/{job_id}` is
 * polled every 1.5s and the job's log lines render as a terminal-style feed
 * (the backend mirrors the CLI's phrasing, so it reads like scripts/ingest.py).
 *
 * State notes:
 *  - Job state lives in this component, not the modal, so closing the dialog
 *    mid-run keeps polling in the background - the rail button shows a spinner
 *    and reopening resumes the feed where it is.
 *  - A 409 from `startIngest` (demo namespace is read-only, or another job is
 *    running) carries an actionable message from the backend; it is rendered
 *    verbatim rather than translated.
 *  - `onIngested` fires exactly once per finished job so the app can refetch
 *    the graph.
 *
 * Modal semantics match the other dialogs: `aria-modal`, labelled, Escape to
 * dismiss, focus trap, backdrop click-to-close, body scroll lock.
 */

import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';

import {
  Badge,
  GlassCard,
  IconButton,
  MicroLabel,
  Spinner,
  cx,
  useBodyScrollLock,
  useEnterTransition,
  useEscapeKey,
  useFocusTrap,
} from './ui';
import { ApiError, api, isAbortError } from '@/lib/api';
import type { IngestJob } from '@/lib/types';

export interface RepoIngestProps {
  /** Called once a job finishes so App can refetch the graph. */
  onIngested?: () => void;
  className?: string;
}

const POLL_INTERVAL_MS = 1_500;

/** Consecutive poll failures tolerated before the job is declared unreachable. */
const MAX_POLL_FAILURES = 4;

type Phase = 'idle' | 'starting' | 'running' | 'done' | 'error';

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message; // backend text, verbatim
  if (error instanceof Error) return error.message;
  return String(error);
}

export function RepoIngest({ onIngested, className }: RepoIngestProps) {
  const titleId = useId();
  const inputId = useId();

  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<IngestJob | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Latest callback without retriggering the poll effect (same pattern as
  // useEscapeKey in ui.tsx).
  const onIngestedRef = useRef(onIngested);
  onIngestedRef.current = onIngested;
  const notifiedRef = useRef(false);

  const dialogRef = useFocusTrap<HTMLDivElement>(open);
  const entered = useEnterTransition(open);
  useEscapeKey(open, () => setOpen(false));
  useBodyScrollLock(open);

  const busy = phase === 'starting' || phase === 'running';

  const submit = useCallback(async (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed) return;
    notifiedRef.current = false;
    setJob(null);
    setJobId(null);
    setErrorMessage(null);
    setPhase('starting');
    try {
      const { job_id } = await api.startIngest({ target: trimmed });
      setJobId(job_id);
      setPhase('running');
    } catch (error) {
      // Includes the 409s: "demo namespace is read-only" / "job busy" - the
      // backend's message explains what to do, so show it as-is.
      setErrorMessage(describeError(error));
      setPhase('error');
    }
  }, []);

  // Poll the active job every 1.5s until it leaves "running".
  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    let failures = 0;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const next = await api.getIngestJob(jobId, { signal: controller.signal });
        if (cancelled) return;
        failures = 0;
        setJob(next);
        if (next.status === 'running') return;
        window.clearInterval(timer);
        if (next.status === 'done') {
          setPhase('done');
          if (!notifiedRef.current) {
            notifiedRef.current = true;
            onIngestedRef.current?.();
          }
        } else {
          setErrorMessage(next.error ?? 'Ingestion failed');
          setPhase('error');
        }
      } catch (error) {
        if (cancelled || isAbortError(error)) return;
        failures += 1;
        const gone = error instanceof ApiError && error.status === 404;
        if (gone || failures >= MAX_POLL_FAILURES) {
          window.clearInterval(timer);
          setErrorMessage(describeError(error));
          setPhase('error');
        }
      }
    };

    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [jobId]);

  // Terminal feed sticks to the newest line.
  const feedRef = useRef<HTMLDivElement>(null);
  const logLength = job?.log.length ?? 0;
  useEffect(() => {
    const feed = feedRef.current;
    if (feed) feed.scrollTop = feed.scrollHeight;
  }, [logLength, phase]);

  const reset = useCallback(() => {
    notifiedRef.current = false;
    setJob(null);
    setJobId(null);
    setErrorMessage(null);
    setPhase('idle');
  }, []);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!busy) void submit(target);
  };

  const report = job?.status === 'done' ? job.report : undefined;

  return (
    <>
      {/* --- Rail button --------------------------------------------------- */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label="Add a repository to the graph"
        className={cx(
          'glass glass-sheen glass-interactive flex w-full items-center justify-center gap-2',
          'px-3 py-2 text-2xs font-semibold tracking-[0.14em] text-ink-muted',
          'transition-all duration-200 ease-swift hover:text-ink',
          'focus-visible:outline-none active:scale-[0.98]',
          className,
        )}
      >
        {busy ? <Spinner size={12} label="Ingesting repository" /> : <GlyphPlus />}
        ADD REPO
      </button>

      {/* --- Modal --------------------------------------------------------- */}
      {open ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 max-sm:pb-[max(1rem,env(safe-area-inset-bottom))] sm:p-6"
          role="presentation"
        >
          <div
            className={cx(
              'absolute inset-0 bg-deep/80 backdrop-blur-sm transition-opacity duration-300',
              entered ? 'opacity-100' : 'opacity-0',
            )}
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />

          <GlassCard
            ref={dialogRef}
            raised
            accent="cyan"
            glow
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            tabIndex={-1}
            className={cx(
              'relative flex max-h-full w-full max-w-xl flex-col overflow-hidden outline-none',
              'transition-[transform,opacity] duration-300 ease-swift',
            )}
            style={{
              transform: entered
                ? 'translate3d(0,0,0) scale(1)'
                : 'translate3d(0,12px,0) scale(0.985)',
              opacity: entered ? 1 : 0,
            }}
          >
            {/* Header */}
            <header className="flex items-start gap-3 border-b border-white/10 px-5 py-4">
              <span
                className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-cyan/40 bg-cyan/10 text-cyan"
                aria-hidden="true"
              >
                <GlyphRepo />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <MicroLabel>Repository Ingestion</MicroLabel>
                  {phase === 'running' || phase === 'starting' ? (
                    <Badge accent="cyan" pulse>
                      INGESTING
                    </Badge>
                  ) : phase === 'done' ? (
                    <Badge accent="green">DONE</Badge>
                  ) : phase === 'error' ? (
                    <Badge accent="red">ERROR</Badge>
                  ) : (
                    <Badge accent="slate">READY</Badge>
                  )}
                </div>
                <h2 id={titleId} className="mt-1.5 text-sm font-semibold leading-snug text-ink">
                  Add a repository to the graph
                </h2>
                <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                  Lockfiles are scanned, enriched from the registry and OSV, then written to
                  HydraDB.
                </p>
              </div>
              <IconButton
                aria-label="Close ingestion dialog"
                onClick={() => setOpen(false)}
                className="max-sm:h-10 max-sm:w-10"
              >
                <GlyphClose />
              </IconButton>
            </header>

            {/* Body */}
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
              {/* Below sm the input and submit stack full-width at a 44px touch
                  height; from sm up the max-sm:* classes are inert and the row
                  renders exactly as before. */}
              <form
                onSubmit={onSubmit}
                className="flex items-center gap-2 max-sm:flex-col max-sm:items-stretch"
              >
                <label htmlFor={inputId} className="sr-only">
                  Repository git URL or local path
                </label>
                <input
                  id={inputId}
                  type="text"
                  value={target}
                  onChange={(event) => setTarget(event.target.value)}
                  placeholder="https://github.com/org/repo or /local/path"
                  spellCheck={false}
                  autoComplete="off"
                  disabled={busy}
                  className={cx(
                    'min-w-0 flex-1 rounded-lg border border-white/10 bg-black/40 px-3 py-2',
                    'font-mono text-xs text-ink placeholder:text-ink-faint',
                    'transition-colors duration-200 focus:border-cyan/50 focus:outline-none',
                    'disabled:opacity-50 max-sm:min-h-[44px]',
                  )}
                />
                <button
                  type="submit"
                  disabled={busy || target.trim() === ''}
                  className={cx(
                    'shrink-0 rounded-lg border border-cyan/40 bg-cyan/10 px-3.5 py-2',
                    'text-2xs font-semibold tracking-wide text-cyan',
                    'transition-all duration-200 ease-swift hover:border-cyan/70 hover:bg-cyan/20',
                    'active:scale-95 disabled:pointer-events-none disabled:opacity-40',
                    'max-sm:flex max-sm:min-h-[44px] max-sm:items-center max-sm:justify-center',
                  )}
                >
                  {busy ? <Spinner size={12} label="Starting ingestion" /> : 'INGEST'}
                </button>
              </form>

              {/* Terminal feed */}
              {job !== null || busy ? (
                <div
                  ref={feedRef}
                  aria-live="polite"
                  aria-label="Ingestion progress log"
                  className={cx(
                    'max-h-56 overflow-y-auto rounded-xl border border-white/10 bg-black/50',
                    'px-3 py-2.5 font-mono text-[11px] leading-[1.65] text-ink-muted',
                  )}
                >
                  {(job?.log ?? []).map((line, index) => (
                    <div key={index} className="whitespace-pre-wrap break-words">
                      {line === '' ? ' ' : line}
                    </div>
                  ))}
                  {phase === 'starting' || phase === 'running' ? (
                    <div className="flex items-center gap-2 pt-1 text-cyan">
                      <Spinner size={11} label="Ingestion in progress" />
                      <span>{phase === 'starting' ? 'starting job…' : 'working…'}</span>
                    </div>
                  ) : null}
                </div>
              ) : null}

              {/* Done: summary chips */}
              {report ? (
                <div className="flex flex-wrap items-center gap-2">
                  <Badge accent="green" variant="solid">
                    {report.packages} PACKAGES
                  </Badge>
                  <Badge accent="green" variant="solid">
                    {report.advisories} ADVISORIES
                  </Badge>
                  <Badge accent={report.malicious > 0 ? 'red' : 'green'} variant="solid">
                    {report.malicious} MALICIOUS
                  </Badge>
                </div>
              ) : null}

              {/* Error: backend message verbatim + retry */}
              {phase === 'error' && errorMessage ? (
                <div className="rounded-xl border border-alert/40 bg-alert/10 px-3 py-2.5">
                  <p className="break-words text-xs leading-relaxed text-alert">{errorMessage}</p>
                  <button
                    type="button"
                    onClick={() => void submit(target)}
                    disabled={target.trim() === ''}
                    className={cx(
                      'mt-2 rounded-lg border border-alert/45 bg-alert/10 px-3 py-1.5',
                      'text-2xs font-semibold tracking-wide text-alert',
                      'transition-all duration-200 ease-swift hover:border-alert/70 hover:bg-alert/20',
                      'active:scale-95 disabled:pointer-events-none disabled:opacity-40',
                      'max-sm:flex max-sm:min-h-[44px] max-sm:w-full max-sm:items-center max-sm:justify-center',
                    )}
                  >
                    RETRY
                  </button>
                </div>
              ) : null}
            </div>

            {/* Footer */}
            {/* Same mobile treatment as the patch modal footer: stacked
                full-width actions below sm, untouched from sm up. */}
            <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-white/10 px-5 py-3 max-sm:flex-col max-sm:items-stretch">
              <span className="text-2xs text-ink-faint">
                One job runs at a time - ingestion is registry and OSV heavy.
              </span>
              <div className="flex items-center gap-2 max-sm:flex-col max-sm:items-stretch">
                {phase === 'done' || phase === 'error' ? (
                  <button
                    type="button"
                    onClick={reset}
                    className={cx(
                      'rounded-lg border border-white/10 bg-white/[0.04] px-3.5 py-1.5 text-2xs',
                      'font-medium text-ink-muted transition-all duration-200 ease-swift',
                      'hover:border-white/25 hover:bg-white/[0.08] hover:text-ink active:scale-95',
                      'max-sm:flex max-sm:min-h-[44px] max-sm:items-center max-sm:justify-center',
                    )}
                  >
                    NEW JOB
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => setOpen(false)}
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
      ) : null}
    </>
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

function GlyphPlus() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function GlyphRepo() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M6 3h12a1 1 0 0 1 1 1v16l-4-3-4 3-4-3-2 1.5V4a1 1 0 0 1 1-1Z" />
      <path d="M9 8h6M9 12h4" />
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

export default RepoIngest;
