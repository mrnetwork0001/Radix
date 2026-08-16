/**
 * Slide-in inspector for the selected graph node.
 *
 * `GraphNode` is a flattened union - the backend omits the fields that do not
 * apply to a node's `label` (docs/API_CONTRACT.md), so every type-specific
 * section here branches on `label` rather than probing for a field.
 *
 * The panel is deliberately *non-modal*: it does not trap focus, because the
 * graph behind it stays interactive. It does close on Escape and moves focus to
 * itself when the selection changes, so keyboard and screen-reader users land
 * on the new content.
 */

import { useEffect, useMemo, useRef, type ReactNode } from 'react';

import {
  ACCENTS,
  Badge,
  Divider,
  GlassCard,
  IconButton,
  MetaRow,
  MicroLabel,
  Spinner,
  accentForLabel,
  cx,
  formatCompact,
  formatNumber,
  formatTimestamp,
  useEnterTransition,
  useEscapeKey,
} from './ui';
import type { Accent } from './ui';
import type {
  ClosureResponse,
  GraphNode,
  MaintainerRisk,
  TyposquatCandidate,
} from '@/lib/types';

export interface NodeInspectorProps {
  /** Nothing renders when null - the panel is unmounted, not hidden. */
  node: GraphNode | null;
  /** Latest closure, used for depth, exposure and lockfile paths. */
  closure?: ClosureResponse | null;
  maintainerRisk?: MaintainerRisk | null;
  typosquats?: TyposquatCandidate[] | null;
  onClose: () => void;
  /** Receives the node the patch is being generated for. */
  onGeneratePatch: (node: GraphNode) => void;
  loading?: boolean;
  className?: string;
}

// ---------------------------------------------------------------------------
// Derivations
// ---------------------------------------------------------------------------

/**
 * Hop count from the compromised root, read off the `SSpaths` chains.
 * Paths are ordered root-first, so a node's index *is* its depth; the smallest
 * index across all paths is the shortest infection route.
 */
function depthInClosure(id: number, closure: ClosureResponse | null): number | null {
  if (!closure) return null;
  if (closure.root.id === id) return 0;
  let shallowest: number | null = null;
  for (const path of closure.paths) {
    const index = path.indexOf(id);
    if (index >= 0 && (shallowest === null || index < shallowest)) shallowest = index;
  }
  return shallowest;
}

function isExposed(id: number, closure: ClosureResponse | null): boolean {
  if (!closure) return false;
  return (
    closure.affected_package_ids.includes(id) ||
    closure.affected_service_ids.includes(id) ||
    closure.affected_lockfile_ids.includes(id)
  );
}

/** A patch is only meaningful for the breach root or something it reached. */
function canGeneratePatch(node: GraphNode, closure: ClosureResponse | null): boolean {
  if (node.label === 'Package') return node.is_compromised === true || closure?.root.id === node.id;
  if (node.label === 'Service') return closure?.affected_service_ids.includes(node.id) ?? false;
  if (node.label === 'Lockfile') return closure?.affected_lockfile_ids.includes(node.id) ?? false;
  return false;
}

function riskAccent(score: number): 'green' | 'amber' | 'red' {
  if (score >= 0.7) return 'red';
  if (score >= 0.35) return 'amber';
  return 'green';
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function NodeInspector({
  node,
  closure = null,
  maintainerRisk = null,
  typosquats = null,
  onClose,
  onGeneratePatch,
  loading = false,
  className,
}: NodeInspectorProps) {
  const panelRef = useRef<HTMLElement>(null);
  const entered = useEnterTransition(node !== null);

  useEscapeKey(node !== null, onClose);

  const nodeId = node?.id ?? null;
  useEffect(() => {
    if (nodeId === null) return;
    panelRef.current?.focus({ preventScroll: true });
  }, [nodeId]);

  const accent = node ? accentForLabel(node.label) : 'cyan';
  const tone = ACCENTS[accent];

  const depth = useMemo(() => (node ? depthInClosure(node.id, closure) : null), [node, closure]);
  const exposed = node ? isExposed(node.id, closure) : false;
  const patchable = node ? canGeneratePatch(node, closure) : false;

  const affectedLockfiles = useMemo(
    () => (closure?.affected_nodes ?? []).filter((n) => n.label === 'Lockfile'),
    [closure],
  );

  const squats = typosquats ?? [];

  if (!node) return null;

  const compromised = node.is_compromised === true;
  const isRoot = closure?.root.id === node.id;

  return (
    <aside
      // Below sm the panel is a full-width, edge-to-edge sheet; from sm up the
      // max-sm:* classes are inert and this is exactly the previous rendering.
      className={cx(
        'pointer-events-none fixed inset-y-0 right-0 z-40 flex w-full max-w-[27rem] p-3',
        'max-sm:inset-x-0 max-sm:max-w-none max-sm:p-0',
        className,
      )}
      aria-label="Node inspector"
    >
      <GlassCard
        ref={panelRef}
        raised
        accent={compromised ? 'red' : accent}
        glow={compromised}
        tabIndex={-1}
        className={cx(
          'pointer-events-auto flex h-full w-full flex-col overflow-hidden outline-none',
          'transition-[transform,opacity] duration-300 ease-swift',
          // Full-bleed sheet: square corners and safe-area padding so the
          // header and footer clear the notch and the home indicator.
          'max-sm:rounded-none max-sm:pt-[env(safe-area-inset-top)] max-sm:pb-[env(safe-area-inset-bottom)]',
        )}
        style={{
          transform: entered ? 'translate3d(0,0,0)' : 'translate3d(28px,0,0)',
          opacity: entered ? 1 : 0,
        }}
      >
        {/* --- Header ---------------------------------------------------- */}
        <header className="flex items-start gap-3 border-b border-white/10 px-4 py-3.5">
          <span
            className={cx(
              'mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border',
              tone.border,
              tone.fill,
              tone.text,
              compromised && 'halo-alert',
            )}
            aria-hidden="true"
          >
            <LabelGlyph label={node.label} />
          </span>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge accent={compromised ? 'red' : accent} variant="outline">
                {node.label.toUpperCase()}
              </Badge>
              {isRoot ? (
                <Badge accent="red" variant="solid" pulse>
                  BREACH ROOT
                </Badge>
              ) : null}
              {compromised && !isRoot ? (
                <Badge accent="red" variant="solid">
                  COMPROMISED
                </Badge>
              ) : null}
              {exposed && !isRoot ? (
                <Badge accent="amber" variant="soft">
                  EXPOSED
                </Badge>
              ) : null}
            </div>

            <h2 className="mt-1.5 break-words text-base font-semibold leading-tight text-ink">
              {node.name}
            </h2>
            <p className="stat-numeral mt-0.5 font-mono text-[11px] text-ink-faint">
              vertex #{node.id}
            </p>
          </div>

          <IconButton
            aria-label="Close inspector"
            onClick={onClose}
            className="max-sm:h-10 max-sm:w-10"
          >
            <GlyphClose />
          </IconButton>
        </header>

        {/* --- Body ------------------------------------------------------ */}
        <div
          key={node.id}
          className="rise-in min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4 scrollbar-none"
        >
          {loading ? (
            <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
              <Spinner size={14} accent={accent} label="Loading node detail" />
              <span className="text-2xs text-ink-muted">resolving graph context…</span>
            </div>
          ) : null}

          {/* Label-specific metadata. */}
          <Section title="Metadata">
            {node.label === 'Package' ? <PackageMeta node={node} /> : null}
            {node.label === 'Version' ? <VersionMeta node={node} /> : null}
            {node.label === 'Maintainer' ? <MaintainerMeta node={node} /> : null}
            {node.label === 'Service' ? <ServiceMeta node={node} /> : null}
            {node.label === 'Lockfile' ? <LockfileMeta node={node} /> : null}
          </Section>

          {/* Exposure. */}
          {closure ? (
            <Section title="Exposure" accent={exposed || isRoot ? 'red' : 'slate'}>
              <MetaRow label="Transitive depth">
                {depth === null ? (
                  <span className="text-ink-faint">outside the closure</span>
                ) : depth === 0 ? (
                  <span className="text-alert">origin (0 hops)</span>
                ) : (
                  <span className="stat-numeral">
                    {depth} {depth === 1 ? 'hop' : 'hops'} from root
                  </span>
                )}
              </MetaRow>
              <MetaRow label="Closure root">
                <span className="font-mono text-[11px] text-cyan">{closure.root.name}</span>
              </MetaRow>
              <MetaRow label="Walk bound">
                <span className="font-mono text-[11px] text-ink-muted">{`*1..${closure.depth}`}</span>
              </MetaRow>
              <MetaRow label="Traversal">
                <span className="stat-numeral text-cyan">
                  {formatNumber(closure.latency_ms, 1)} ms
                </span>
              </MetaRow>
            </Section>
          ) : null}

          {/* Lockfile paths reached by the closure. */}
          {affectedLockfiles.length > 0 ? (
            <Section title={`Lockfile paths (${affectedLockfiles.length})`} accent="amber">
              <ul className="space-y-1.5">
                {affectedLockfiles.map((lock) => (
                  <li
                    key={lock.id}
                    className={cx(
                      'flex items-center gap-2 rounded-lg border px-2.5 py-1.5',
                      lock.id === node.id
                        ? 'border-amber/40 bg-amber/10'
                        : 'border-white/10 bg-white/[0.02]',
                    )}
                  >
                    <span className="text-amber" aria-hidden="true">
                      <GlyphFile />
                    </span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-muted">
                      {lock.name}
                      {lock.filename && !lock.name.includes(lock.filename) ? (
                        <span className="text-ink-faint">/{lock.filename}</span>
                      ) : null}
                    </span>
                    {lock.commit_hash ? (
                      <span className="stat-numeral shrink-0 font-mono text-[10px] text-ink-faint">
                        {lock.commit_hash.slice(0, 7)}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* Maintainer history. */}
          {maintainerRisk ? (
            <Section title="Maintainer history" accent="violet">
              <MetaRow label="Account">
                <span className="font-mono text-[11px] text-violet">
                  {maintainerRisk.maintainer.username ?? maintainerRisk.maintainer.name}
                </span>
              </MetaRow>
              {maintainerRisk.maintainer.email ? (
                <MetaRow label="Email" mono>
                  {maintainerRisk.maintainer.email}
                </MetaRow>
              ) : null}
              {maintainerRisk.maintainer.key_fingerprint ? (
                <MetaRow label="Signing key" mono>
                  {maintainerRisk.maintainer.key_fingerprint}
                </MetaRow>
              ) : null}

              {maintainerRisk.risk_note ? (
                <p className="mt-2 rounded-lg border border-violet/25 bg-violet/10 px-3 py-2 text-xs leading-relaxed text-ink-muted">
                  {maintainerRisk.risk_note}
                </p>
              ) : null}

              {maintainerRisk.sister_packages.length > 0 ? (
                <>
                  <Divider className="my-3" />
                  <MicroLabel className="mb-2">
                    Also publishes ({maintainerRisk.sister_packages.length})
                  </MicroLabel>
                  <ul className="space-y-1.5">
                    {maintainerRisk.sister_packages.map((sister) => (
                      <li
                        key={sister.id}
                        className={cx(
                          'flex items-center gap-2 rounded-lg border px-2.5 py-1.5',
                          sister.flagged
                            ? 'border-alert/40 bg-alert/10'
                            : 'border-white/10 bg-white/[0.02]',
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-muted">
                          {sister.name}
                        </span>
                        {sister.published_within_window ? (
                          <Badge accent="amber">IN WINDOW</Badge>
                        ) : null}
                        {sister.flagged ? <Badge accent="red">FLAGGED</Badge> : null}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </Section>
          ) : null}

          {/* Typosquats. */}
          {squats.length > 0 ? (
            <Section title={`Typosquat candidates (${squats.length})`} accent="red">
              <ul className="space-y-1.5">
                {squats.map((squat) => (
                  <li
                    key={squat.id}
                    className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.02] px-2.5 py-1.5"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-muted">
                      {squat.name}
                    </span>
                    <span className="stat-numeral shrink-0 text-[10px] text-ink-faint">
                      d={squat.edit_distance}
                    </span>
                    <Badge accent={squat.similarity_score >= 0.9 ? 'red' : 'amber'}>
                      {formatNumber(squat.similarity_score * 100, 0)}%
                    </Badge>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}
        </div>

        {/* --- Footer action --------------------------------------------- */}
        {patchable ? (
          <footer className="border-t border-white/10 px-4 py-3">
            <button
              type="button"
              onClick={() => onGeneratePatch(node)}
              disabled={loading}
              className={cx(
                'group relative flex w-full items-center justify-center gap-2 overflow-hidden rounded-xl',
                'border border-toxic/45 bg-gradient-to-br from-toxic/20 via-toxic/10 to-transparent',
                'px-4 py-2.5 text-xs font-semibold tracking-wide text-toxic max-sm:min-h-[44px]',
                'shadow-glow-green transition-all duration-200 ease-swift',
                'hover:-translate-y-px hover:border-toxic/75 hover:from-toxic/30',
                'active:translate-y-0 active:scale-[0.99]',
                'disabled:pointer-events-none disabled:opacity-50',
              )}
            >
              <GlyphPatch />
              GENERATE PATCH PR
            </button>
            <p className="mt-1.5 text-center text-[10px] text-ink-faint">
              pins the safe version and rewrites every exposed lockfile
            </p>
          </footer>
        ) : null}
      </GlassCard>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function Section({
  title,
  accent = 'slate',
  children,
}: {
  title: string;
  accent?: Accent;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-1.5 flex items-center gap-2">
        <span
          className="h-3 w-[2px] rounded-full"
          style={{ background: `rgb(${ACCENTS[accent].rgb} / 0.9)` }}
          aria-hidden="true"
        />
        <MicroLabel>{title}</MicroLabel>
      </div>
      <div className="rounded-xl border border-white/10 bg-white/[0.02] px-3 py-1.5">{children}</div>
    </section>
  );
}

function PackageMeta({ node }: { node: GraphNode }) {
  const score = node.risk_score ?? 0;
  const accent = riskAccent(score);
  return (
    <>
      <MetaRow label="Ecosystem">
        <span className="font-mono text-[11px]">{node.ecosystem ?? 'npm'}</span>
      </MetaRow>
      <MetaRow label="Weekly downloads">
        <span className="stat-numeral">{formatCompact(node.downloads_weekly ?? 0)}</span>
      </MetaRow>
      <MetaRow label="Compromised">
        {node.is_compromised ? (
          <span className="text-alert">yes - malicious release</span>
        ) : (
          <span className="text-toxic">no</span>
        )}
      </MetaRow>
      <div className="py-1.5">
        <div className="flex items-center justify-between">
          <span className="label-micro">Risk score</span>
          <span className={cx('stat-numeral text-xs', ACCENTS[accent].text)}>
            {formatNumber(score, 2)}
          </span>
        </div>
        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full border border-white/10 bg-white/[0.05]">
          <div
            className="h-full rounded-full transition-[width] duration-500 ease-swift"
            style={{
              width: `${Math.min(100, Math.max(0, score * 100))}%`,
              background: `linear-gradient(90deg, rgb(${ACCENTS[accent].rgb} / 0.45), rgb(${ACCENTS[accent].rgb} / 1))`,
            }}
          />
        </div>
      </div>
    </>
  );
}

function VersionMeta({ node }: { node: GraphNode }) {
  return (
    <>
      <MetaRow label="Semver">
        <span className="font-mono text-[11px] text-cyan">{node.semver ?? node.name}</span>
      </MetaRow>
      <MetaRow label="Published">{formatTimestamp(node.published_at)}</MetaRow>
      <MetaRow label="Breach window">
        {node.compromised_window ? (
          <span className="text-alert">inside the compromise window</span>
        ) : (
          <span className="text-toxic">outside</span>
        )}
      </MetaRow>
      <MetaRow label="Ecosystem">
        <span className="font-mono text-[11px]">{node.ecosystem ?? 'npm'}</span>
      </MetaRow>
    </>
  );
}

function MaintainerMeta({ node }: { node: GraphNode }) {
  return (
    <>
      <MetaRow label="Username">
        <span className="font-mono text-[11px] text-violet">{node.username ?? node.name}</span>
      </MetaRow>
      {node.email ? (
        <MetaRow label="Email" mono>
          {node.email}
        </MetaRow>
      ) : null}
      {node.key_fingerprint ? (
        <MetaRow label="Key fingerprint" mono>
          {node.key_fingerprint}
        </MetaRow>
      ) : null}
    </>
  );
}

function ServiceMeta({ node }: { node: GraphNode }) {
  const tier1 = node.criticality === 'tier-1';
  return (
    <>
      <MetaRow label="Criticality">
        <Badge accent={tier1 ? 'red' : 'green'} variant="outline">
          {(node.criticality ?? 'unclassified').toUpperCase()}
        </Badge>
      </MetaRow>
      {node.repo_url ? (
        <MetaRow label="Repository" mono>
          {node.repo_url}
        </MetaRow>
      ) : null}
    </>
  );
}

function LockfileMeta({ node }: { node: GraphNode }) {
  return (
    <>
      <MetaRow label="Filename">
        <span className="font-mono text-[11px] text-amber">
          {node.filename ?? node.name}
        </span>
      </MetaRow>
      {node.commit_hash ? (
        <MetaRow label="Commit" mono>
          {node.commit_hash}
        </MetaRow>
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

function LabelGlyph({ label }: { label: GraphNode['label'] }) {
  switch (label) {
    case 'Package':
      return (
        <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
          <path d="M12 2.8 3.5 7.2v9.6L12 21.2l8.5-4.4V7.2L12 2.8Z" />
          <path d="M3.5 7.2 12 11.6l8.5-4.4M12 11.6v9.6" />
        </svg>
      );
    case 'Version':
      return (
        <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7.5v5l3 2" />
        </svg>
      );
    case 'Maintainer':
      return (
        <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
          <circle cx="12" cy="8.5" r="3.6" />
          <path d="M4.8 20a7.2 7.2 0 0 1 14.4 0" />
        </svg>
      );
    case 'Service':
      return (
        <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
          <rect x="3.2" y="4.2" width="17.6" height="6.2" rx="2" />
          <rect x="3.2" y="13.6" width="17.6" height="6.2" rx="2" />
          <path d="M7 7.3h.01M7 16.7h.01" />
        </svg>
      );
    case 'Lockfile':
      return (
        <svg width="17" height="17" viewBox="0 0 24 24" {...strokeProps}>
          <path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5L13.5 3Z" />
          <path d="M13.5 3v5.5H19" />
        </svg>
      );
  }
}

function GlyphClose() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

function GlyphFile() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5L13.5 3Z" />
    </svg>
  );
}

function GlyphPatch() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" {...strokeProps}>
      <path d="M4 17.5 9.5 12 4 6.5M12.5 18H20" />
    </svg>
  );
}

export default NodeInspector;
