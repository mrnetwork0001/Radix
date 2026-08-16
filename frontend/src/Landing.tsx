/**
 * Radix landing page.
 *
 * Served at `/`; the dashboard lives behind the Launch App button at `/app`
 * and is code-split so none of its weight (force-graph included) loads here.
 * Every number on this page is a real measurement from the verification runs —
 * the page's job is to make the claim, the console's job is to prove it.
 */

import type { ReactNode, SVGProps } from 'react';

import { Badge, GlassCard, cx } from './components/ui';

const GITHUB_URL = 'https://github.com/mrnetwork0001/Radix';

export interface LandingProps {
  onLaunch: () => void;
}

export default function Landing({ onLaunch }: LandingProps) {
  return (
    <div className="relative min-h-screen overflow-x-hidden text-slate-200">
      <a
        href="/app"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50"
      >
        Skip to the console
      </a>

      <TopNav />

      <main className="mx-auto w-full max-w-6xl px-6">
        <Hero onLaunch={onLaunch} />
        <MeasuredStats />
        <Features />
        <GraphNotVector />
        <HowItWorks />
        <FinalCta onLaunch={onLaunch} />
      </main>

      <Footer onLaunch={onLaunch} />
    </div>
  );
}

/* ── Chrome ──────────────────────────────────────────────────────────────── */

function TopNav() {
  return (
    <header className="sticky top-0 z-40 border-b border-white/5 bg-deep/70 backdrop-blur-md">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center px-6">
        <a href="/" aria-label="Radix home">
          <Wordmark className="h-5" />
        </a>
      </div>
    </header>
  );
}

function LaunchButton({
  onClick,
  size = 'lg',
  className,
}: {
  onClick: () => void;
  size?: 'sm' | 'lg';
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cx(
        'group relative overflow-hidden rounded-xl border border-cyan/50 font-semibold tracking-wide',
        'bg-gradient-to-br from-cyan/25 via-cyan/10 to-transparent text-cyan shadow-glow-cyan',
        'transition-all duration-200 ease-swift hover:-translate-y-px hover:border-cyan/80 hover:from-cyan/35',
        'focus-visible:outline-none active:translate-y-0 active:scale-[0.99]',
        size === 'lg' ? 'px-6 py-3 text-sm' : 'px-4 py-1.5 text-xs',
        className,
      )}
    >
      {/* Sheen wipe, same idiom as the console's hero action. */}
      <span
        aria-hidden="true"
        className={cx(
          'pointer-events-none absolute inset-y-0 -left-full w-1/2 skew-x-[-18deg]',
          'bg-gradient-to-r from-transparent via-white/20 to-transparent',
          'transition-transform duration-700 ease-swift group-hover:translate-x-[300%]',
        )}
      />
      <span className="relative flex items-center gap-2">
        LAUNCH APP
        <span aria-hidden="true" className="transition-transform duration-200 group-hover:translate-x-0.5">
          →
        </span>
      </span>
    </button>
  );
}

/* ── Hero ────────────────────────────────────────────────────────────────── */

function Hero({ onLaunch }: { onLaunch: () => void }) {
  return (
    <section className="grid items-center gap-12 pb-20 pt-16 sm:pt-24 lg:grid-cols-[1.05fr_0.95fr]">
      <div className="animate-rise-in">
        <Badge accent="red" pulse className="mb-5">
          when a package turns hostile, minutes matter
        </Badge>

        <h1 className="text-balance text-4xl font-bold leading-[1.08] tracking-tight text-white sm:text-5xl">
          Your supply chain has a{' '}
          <span className="text-alert glow-text-red">blast radius</span>.
          <br />
          Radix computes it in{' '}
          <span className="text-cyan glow-text-cyan">milliseconds</span>.
        </h1>

        <p className="mt-6 max-w-xl text-base leading-relaxed text-ink-muted">
          When a compromised release lands on npm, the question is not what the
          package looks like — it is which of your services are transitively
          exposed, through which routes, and what to pin right now. That is a
          graph traversal, and Radix runs it on{' '}
          <a
            href="https://github.com/hydra-db/hydradb"
            target="_blank"
            rel="noreferrer"
            className="text-cyan underline decoration-cyan/40 underline-offset-2 hover:decoration-cyan"
          >
            HydraDB
          </a>{' '}
          — reverse dependency closure, maintainer co-authorship, typosquat
          proximity, and a ready-to-open lockfile patch.
        </p>

        <div className="mt-8 flex flex-wrap items-center gap-4">
          <LaunchButton onClick={onLaunch} />
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className={cx(
              'flex items-center gap-2 rounded-xl border border-white/10 px-5 py-3 text-sm text-ink-muted',
              'transition-colors duration-200 hover:border-white/25 hover:text-slate-200',
            )}
          >
            <GitHubGlyph className="h-4 w-4" />
            Read the source
          </a>
        </div>

        <p className="label-micro mt-6 text-ink-faint">
          open source · MIT · built for Hack Hydra 2026
        </p>
      </div>

      <BlastRadiusFigure />
    </section>
  );
}

/**
 * A dependency graph mid-incident: the compromised root pulses, infection
 * flows outward along the transitive edges, clean nodes hold steady. Pure
 * SVG + the design system's keyframes; transform/opacity/dashoffset only.
 */
function BlastRadiusFigure() {
  // Root (compromised), two hops of infected nodes, plus clean bystanders.
  const infected = [
    { id: 'r', x: 200, y: 150, r: 13 },
    { id: 'a', x: 118, y: 84, r: 8 },
    { id: 'b', x: 300, y: 92, r: 8 },
    { id: 'c', x: 262, y: 232, r: 8 },
    { id: 'd', x: 52, y: 140, r: 6 },
    { id: 'e', x: 352, y: 176, r: 6 },
  ] as const;
  const clean = [
    { x: 96, y: 236, r: 6 },
    { x: 174, y: 40, r: 5 },
    { x: 344, y: 40, r: 5 },
    { x: 30, y: 60, r: 4 },
    { x: 372, y: 260, r: 5 },
    { x: 196, y: 282, r: 5 },
  ] as const;
  type InfectedId = (typeof infected)[number]['id'];
  const infectionEdges: readonly (readonly [InfectedId, InfectedId])[] = [
    ['r', 'a'], ['r', 'b'], ['r', 'c'], ['a', 'd'], ['b', 'e'],
  ];
  const cleanEdges = [
    [96, 236, 118, 84], [174, 40, 118, 84], [344, 40, 300, 92],
    [372, 260, 352, 176], [196, 282, 262, 232], [30, 60, 52, 140],
  ] as const;
  const pos = Object.fromEntries(infected.map((n) => [n.id, n])) as Record<
    InfectedId,
    (typeof infected)[number]
  >;

  return (
    <figure className="animate-rise-in [animation-delay:120ms]" aria-hidden="true">
      <GlassCard className="relative p-2">
        <svg viewBox="0 0 400 300" className="h-auto w-full" role="img">
          <title>Infection propagating through a dependency graph</title>

          {/* Bystander edges first, so infection routes draw above them. */}
          {cleanEdges.map(([x1, y1, x2, y2], i) => (
            <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} stroke="rgb(255 255 255 / 0.08)" strokeWidth="1" />
          ))}

          {infectionEdges.map(([from, to]) => {
            const f = pos[from];
            const t = pos[to];
            return (
              <line
                key={`${from}${to}`}
                x1={f.x} y1={f.y} x2={t.x} y2={t.y}
                stroke="rgb(var(--red-rgb) / 0.6)"
                strokeWidth="1.5"
                strokeDasharray="6 6"
                style={{ animation: 'flow-dash-stroke 0.9s linear infinite' }}
              />
            );
          })}

          {clean.map((n, i) => (
            <circle key={i} cx={n.x} cy={n.y} r={n.r} fill="rgb(var(--cyan-rgb) / 0.75)" />
          ))}

          {infected.map((n) => (
            <g key={n.id}>
              {/* Halo: breathe animates opacity only, so it composites. */}
              <circle
                cx={n.x} cy={n.y} r={n.r * 2.1}
                fill="rgb(var(--red-rgb) / 0.16)"
                className="animate-breathe"
                style={{ animationDelay: `${(n.x + n.y) % 900}ms` }}
              />
              <circle cx={n.x} cy={n.y} r={n.r} fill="rgb(var(--red-rgb) / 0.9)" />
            </g>
          ))}

          <text x={pos.r.x + 20} y={pos.r.y + 4} className="fill-[rgb(var(--red-rgb))] font-mono text-[10px]">
            compromised@4.28.0
          </text>
        </svg>

        <figcaption className="flex items-center justify-between border-t border-white/5 px-3 py-2">
          <span className="label-micro">reverse closure · DEPENDED_ON_BY*1..6</span>
          <span className="stat-numeral text-xs text-cyan">traversal: ~6 ms</span>
        </figcaption>
      </GlassCard>
    </figure>
  );
}

/* ── Measured stats ──────────────────────────────────────────────────────── */

const STATS = [
  { value: '~6 ms', label: 'reverse closure, depth 6', accent: 'text-cyan' },
  { value: '35%', label: 'blast radius pinpointed — 7 of 20 services', accent: 'text-alert' },
  { value: '82', label: 'infection paths returned, longest 7 hops', accent: 'text-toxic' },
  { value: '0.31 s', label: 'to seed 502 nodes and 3,877 edges', accent: 'text-amber' },
] as const;

function MeasuredStats() {
  return (
    <section aria-label="Measured results" className="pb-20">
      <GlassCard raised className="grid grid-cols-2 gap-px overflow-hidden p-0 lg:grid-cols-4">
        {STATS.map((s) => (
          <div key={s.label} className="bg-white/[0.02] px-6 py-5">
            <div className={cx('stat-numeral text-3xl font-bold', s.accent)}>{s.value}</div>
            <div className="mt-1 text-xs leading-snug text-ink-muted">{s.label}</div>
          </div>
        ))}
      </GlassCard>
      <p className="label-micro mt-3 text-center text-ink-faint">
        measured on the bundled 502-node incident graph — reproduce with{' '}
        <code className="text-ink-muted">make up && make seed && make verify</code>
      </p>
    </section>
  );
}

/* ── Features ────────────────────────────────────────────────────────────── */

const FEATURES = [
  {
    title: 'Reverse closure engine',
    accent: 'cyan',
    glyph: RadiatingGlyph,
    body: 'One traversal answers the incident question: every package, service and lockfile transitively exposed to a compromised release — with the exact hop-by-hop routes, not a similarity score.',
  },
  {
    title: 'Maintainer sentinel',
    accent: 'violet',
    glyph: KeyGlyph,
    body: 'A stolen signing key rarely ships one package. Radix walks the co-authorship subgraph and flags the sister packages published inside the breach window — before anyone reports them.',
  },
  {
    title: 'Typosquat radar',
    accent: 'amber',
    glyph: MaskGlyph,
    body: 'Levenshtein and homoglyph neighbours of your high-download dependencies, precomputed into the graph — including the Cyrillic look-alikes your eyes cannot catch in a diff.',
  },
  {
    title: '1-click remediation',
    accent: 'green',
    glyph: PatchGlyph,
    body: 'The last clean release, selected by real semver ordering, rendered as a unified lockfile diff with npm overrides — a PR body ready to paste, generated straight from the graph.',
  },
] as const;

function Features() {
  return (
    <section id="features" aria-label="Capabilities" className="scroll-mt-24 pb-20">
      <SectionHeading
        kicker="what it does"
        title="Four questions, one graph"
        sub="Every capability is a traversal over the same five node types — packages, versions, maintainers, services, lockfiles."
      />
      <div className="grid gap-4 sm:grid-cols-2">
        {FEATURES.map((f) => (
          <GlassCard key={f.title} interactive className="p-6">
            <div className="flex items-start gap-4">
              <span
                className={cx(
                  'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border',
                  f.accent === 'cyan' && 'border-cyan/40 bg-cyan/10 text-cyan',
                  f.accent === 'violet' && 'border-violet/40 bg-violet/10 text-violet',
                  f.accent === 'amber' && 'border-amber/40 bg-amber/10 text-amber',
                  f.accent === 'green' && 'border-toxic/40 bg-toxic/10 text-toxic',
                )}
              >
                <f.glyph className="h-5 w-5" />
              </span>
              <div>
                <h3 className="text-sm font-semibold tracking-wide text-white">{f.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-ink-muted">{f.body}</p>
              </div>
            </div>
          </GlassCard>
        ))}
      </div>
    </section>
  );
}

/* ── Graph vs vector ─────────────────────────────────────────────────────── */

function GraphNotVector() {
  return (
    <section id="why-hydradb" aria-label="Why a graph database" className="scroll-mt-24 pb-20">
      <SectionHeading
        kicker="why HydraDB"
        title="Reachability is not similarity"
        sub="A vector database ranks what resembles the compromised package. None of your exposed services resemble it at all."
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <GlassCard className="p-6 opacity-80">
          <div className="label-micro mb-4 text-ink-faint">vector search</div>
          <ul className="space-y-3 text-sm text-ink-muted">
            <CompareRow no>“what looks similar” — ranked, approximate</CompareRow>
            <CompareRow no>transitive depth is not representable</CompareRow>
            <CompareRow no>92% confidence still means checking every service by hand</CompareRow>
            <CompareRow no>the infection route is lost entirely</CompareRow>
          </ul>
        </GlassCard>
        <GlassCard accent="cyan" glow className="p-6">
          <div className="label-micro mb-4 text-cyan">HydraDB traversal</div>
          <ul className="space-y-3 text-sm text-slate-300">
            <CompareRow>“what is actually reachable” — an exact set</CompareRow>
            <CompareRow>
              native multi-hop:{' '}
              <code className="font-mono text-xs text-cyan">[:DEPENDED_ON_BY*1..6]</code>
            </CompareRow>
            <CompareRow>a service is exposed or it is not — no triage list</CompareRow>
            <CompareRow>whole paths returned, ready to animate and to audit</CompareRow>
          </ul>
        </GlassCard>
      </div>
    </section>
  );
}

function CompareRow({ children, no = false }: { children: ReactNode; no?: boolean }) {
  return (
    <li className="flex items-start gap-3">
      <span
        aria-hidden="true"
        className={cx('mt-0.5 select-none font-mono text-xs', no ? 'text-alert/70' : 'text-toxic')}
      >
        {no ? '✕' : '✓'}
      </span>
      <span>{children}</span>
    </li>
  );
}

/* ── How it works ────────────────────────────────────────────────────────── */

const STEPS = [
  {
    n: '01',
    title: 'Ingest',
    body: 'Point Radix at your repos. Lockfiles (npm, yarn, pnpm) become a graph of packages, versions, maintainers and services — enriched from the registry and the OSV advisory feed.',
  },
  {
    n: '02',
    title: 'Traverse',
    body: 'A compromise fires one bounded traversal in HydraDB. The reverse closure, the maintainer subgraph and the typosquat neighbourhood come back in milliseconds, as data — not a report.',
  },
  {
    n: '03',
    title: 'Remediate',
    body: 'Radix selects the last clean version and renders the lockfile diff and npm overrides per affected service. Review the paths, generate the patch, ship the pin.',
  },
] as const;

function HowItWorks() {
  return (
    <section id="how-it-works" aria-label="How it works" className="scroll-mt-24 pb-20">
      <SectionHeading kicker="how it works" title="Ingest → traverse → remediate" />
      <div className="grid gap-4 lg:grid-cols-3">
        {STEPS.map((s) => (
          <GlassCard key={s.n} className="p-6">
            <div className="stat-numeral text-xs text-cyan/70">{s.n}</div>
            <h3 className="mt-2 text-sm font-semibold tracking-wide text-white">{s.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-ink-muted">{s.body}</p>
          </GlassCard>
        ))}
      </div>

      <GlassCard raised className="mt-4 overflow-x-auto p-0">
        <div className="flex items-center justify-between border-b border-white/5 px-5 py-2.5">
          <span className="label-micro">the incident query</span>
          <Badge accent="cyan" variant="outline">
            OpenCypher · HydraDB
          </Badge>
        </div>
        <pre className="px-5 py-4 font-mono text-xs leading-relaxed text-slate-300">
          <code>{`MATCH (victim {id: $pkg})-[:DEPENDED_ON_BY*1..6]->(dependent)
RETURN DISTINCT dependent.id AS id
-- every transitively exposed node, exact, in single-digit milliseconds`}</code>
        </pre>
      </GlassCard>
    </section>
  );
}

/* ── Final CTA + footer ──────────────────────────────────────────────────── */

function FinalCta({ onLaunch }: { onLaunch: () => void }) {
  return (
    <section aria-label="Get started" className="pb-24">
      <GlassCard accent="cyan" glow raised className="relative overflow-hidden p-10 text-center">
        <h2 className="text-balance text-2xl font-bold tracking-tight text-white sm:text-3xl">
          The next compromised release is already being published.
        </h2>
        <p className="mx-auto mt-3 max-w-lg text-sm leading-relaxed text-ink-muted">
          Open the console, detonate the bundled incident, and watch a six-hop
          blast radius resolve before the animation finishes.
        </p>
        <div className="mt-7 flex flex-wrap items-center justify-center gap-4">
          <LaunchButton onClick={onLaunch} />
          <a
            href={`${GITHUB_URL}#quickstart`}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-ink-muted underline decoration-white/20 underline-offset-4 transition-colors hover:text-slate-200"
          >
            or run it yourself with <code className="font-mono text-xs">make dev</code>
          </a>
        </div>
      </GlassCard>
    </section>
  );
}

interface FooterLink {
  label: string;
  href: string;
  external?: boolean;
}

/**
 * Column contents kept honest: Product links only to things on this page or in
 * the console, Ecosystem only to services Radix actually consumes, Resources
 * only to documents that exist in the repo.
 */
const FOOTER_COLUMNS: ReadonlyArray<{ heading: string; links: readonly FooterLink[] }> = [
  {
    heading: 'Product',
    links: [
      { label: 'Console', href: '/app' },
      { label: 'Capabilities', href: '#features' },
      { label: 'Why HydraDB', href: '#why-hydradb' },
      { label: 'How It Works', href: '#how-it-works' },
    ],
  },
  {
    heading: 'Ecosystem',
    links: [
      { label: 'HydraDB', href: 'https://github.com/hydra-db/hydradb', external: true },
      { label: 'OSV.dev Advisories', href: 'https://osv.dev', external: true },
      { label: 'npm Registry', href: 'https://registry.npmjs.org', external: true },
      { label: 'Hack Hydra 2026', href: 'https://hackhydra.hydradb.com', external: true },
    ],
  },
  {
    heading: 'Resources',
    links: [
      { label: 'GitHub', href: GITHUB_URL, external: true },
      { label: 'Quickstart', href: `${GITHUB_URL}#quickstart`, external: true },
      { label: 'Engine Contract', href: `${GITHUB_URL}/blob/main/docs/HYDRADB_CONTRACT.md`, external: true },
      { label: 'API Reference', href: `${GITHUB_URL}/blob/main/docs/API_CONTRACT.md`, external: true },
      { label: 'Deploy Guide', href: `${GITHUB_URL}/blob/main/deploy/README.md`, external: true },
    ],
  },
];

function Footer({ onLaunch }: { onLaunch: () => void }) {
  return (
    <footer className="border-t border-white/5 bg-deep/40">
      <div className="mx-auto grid w-full max-w-6xl gap-10 px-6 py-12 sm:grid-cols-2 lg:grid-cols-[1.6fr_1fr_1fr_1fr]">
        {/* Brand block */}
        <div className="sm:col-span-2 lg:col-span-1">
          <Wordmark className="h-5" />
          <p className="mt-5 max-w-sm text-sm leading-relaxed text-ink-muted">
            Graph-native supply-chain sentinel on HydraDB. Reverse dependency
            closure, maintainer co-authorship risk, typosquat proximity and
            one-click lockfile remediation — computed in milliseconds, from
            your own lockfiles.
          </p>
          <div className="mt-6">
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noreferrer"
              aria-label="Radix on GitHub"
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-white/10 text-ink-muted transition-colors duration-200 hover:border-white/25 hover:text-slate-200"
            >
              <GitHubGlyph className="h-4 w-4" />
            </a>
          </div>
        </div>

        {FOOTER_COLUMNS.map((col) => (
          <nav key={col.heading} aria-label={col.heading}>
            <div className="label-micro mb-5 text-ink-faint">{col.heading}</div>
            <ul className="space-y-3.5">
              {col.links.map((link) => (
                <li key={link.label}>
                  <a
                    href={link.href}
                    {...(link.external ? { target: '_blank', rel: 'noreferrer' } : {})}
                    {...(link.href === '/app'
                      ? {
                          onClick: (e: { preventDefault: () => void }) => {
                            e.preventDefault();
                            onLaunch();
                          },
                        }
                      : {})}
                    className="font-mono text-sm text-ink-muted transition-colors duration-200 hover:text-cyan"
                  >
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        ))}
      </div>

      <div className="border-t border-white/5">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-5">
          <span className="text-xs text-ink-faint">
            MIT License · built for Hack Hydra 2026
          </span>
          <span className="label-micro text-ink-faint">
            blast radius, computed at traversal speed
          </span>
        </div>
      </div>
    </footer>
  );
}

/* ── Shared bits ─────────────────────────────────────────────────────────── */

function SectionHeading({ kicker, title, sub }: { kicker: string; title: string; sub?: string }) {
  return (
    <div className="mb-8 max-w-2xl">
      <div className="label-micro mb-2 text-cyan">{kicker}</div>
      <h2 className="text-balance text-2xl font-bold tracking-tight text-white sm:text-3xl">{title}</h2>
      {sub ? <p className="mt-3 text-sm leading-relaxed text-ink-muted">{sub}</p> : null}
    </div>
  );
}

function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cx('flex items-center gap-2', className)}>
      <RadiatingGlyph className="h-[1.2em] w-[1.2em] text-cyan" />
      <span className="text-[1em] font-bold uppercase leading-none tracking-[0.28em] text-white">
        Radix
      </span>
    </span>
  );
}

/* Glyphs — 24×24 stroke icons, currentColor so the accent wrapper tints them. */

function RadiatingGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" {...props}>
      <circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="6.5" opacity="0.55" />
      <circle cx="12" cy="12" r="10" opacity="0.25" />
    </svg>
  );
}

function KeyGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" {...props}>
      <circle cx="8.5" cy="8.5" r="4" />
      <path d="M11.5 11.5 20 20M16 16l2.5-2.5M13.5 18.5 16 16" />
    </svg>
  );
}

function MaskGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" {...props}>
      <path d="M4 5c2.5-1.3 5.2-2 8-2s5.5.7 8 2v6.2c0 4.6-3 8.7-8 9.8-5-1.1-8-5.2-8-9.8Z" />
      <path d="M8.5 10.5h.01M15.5 10.5h.01" strokeWidth="2.6" />
      <path d="M9 15c1 .8 2 1.2 3 1.2s2-.4 3-1.2" />
    </svg>
  );
}

function PatchGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" {...props}>
      <path d="M12 3v10M12 13l-3.5-3.5M12 13l3.5-3.5" />
      <path d="M5 16v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

function GitHubGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" {...props}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}
