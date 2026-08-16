<div align="center">

```
        ██████╗  █████╗ ██████╗ ██╗██╗  ██╗
        ██╔══██╗██╔══██╗██╔══██╗██║╚██╗██╔╝
        ██████╔╝███████║██║  ██║██║ ╚███╔╝
        ██╔══██╗██╔══██║██║  ██║██║ ██╔██╗
        ██║  ██║██║  ██║██████╔╝██║██╔╝ ██╗
        ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝╚═╝  ╚═╝

     R E A L - T I M E   S U P P L Y   C H A I N   S E N T I N E L
        ── blast radius, computed at traversal speed ──
```

**Graph-native supply-chain compromise containment, powered by [HydraDB](https://github.com/hydra-db/hydradb).**

*Hack Hydra 2026 · Track 2A — Repos, Dependencies, and Code as Graphs*

</div>

---

## The problem

When a popular npm package is compromised, the clock starts immediately. A
malicious version propagates transitively through lockfiles in minutes, and the
question every security team asks first is not *"what does this package do?"* —
it is:

> **Which of our services are exposed, how deep, and what do we pin to right now?**

That is a **reachability** question. Not a similarity question.

## Why HydraDB, and why not a vector database

A vector store answers *"what is semantically near this?"* by ranking embeddings.
Supply-chain containment needs *"what is reachable from this, transitively, and
by which exact route?"* — and those are different classes of problem. Embedding
`tanstack-query` and asking for its nearest neighbours will faithfully return
packages that *resemble* it. It will never tell you that `auth-microservice` is
compromised through a five-hop chain it has no textual resemblance to at all.

| | Vector search | HydraDB graph traversal |
|---|---|---|
| Answers | "what looks similar" | "what is actually reachable" |
| Transitive depth | not representable | native, `*1..6` in one query |
| Exact blast radius | approximate, ranked | **exact set**, no false positives |
| The infection route | lost | returned as whole paths |
| Recomputation on a new edge | re-embed & re-index | just a write |

Reachability is not an approximation problem, and ranking cannot substitute for
a closure. A dependent service is either exposed or it is not — and if your
remediation tool is 92% confident, you still have to check all 20 services by hand.

Radix pushes traversal into HydraDB and keeps scoring in Python. That split is
deliberate and it maps exactly onto what the engine is good at.

## What Radix does

**1. Transitive reverse-dependency closure.** Given a compromised
`package@version`, return every downstream package, service and lockfile exposed
during the breach window — with the hop-by-hop route to each.

**2. Maintainer co-authorship sentinel.** Walks
`Package → MAINTAINED_BY → Maintainer → MAINTAINS → Other_Package` to surface the
*unflagged sister packages* published by the same compromised signing key inside
the breach window. This is the second wave, and it is invisible to per-package scanning.

**3. Typosquat proximity radar.** Levenshtein and homoglyph neighbours of
high-download packages, precomputed into `TYPOSQUAT_OF` edges with real edit
distances.

**4. Blast radius + one-click remediation.** Exact
`exposed ÷ total × 100%`, and a generated lockfile patch pinning the last clean
version, as a ready-to-open PR diff.

### Measured on the seeded graph

| | |
|---|---|
| Graph | **502 nodes**, 1,975 edges (3,877 stored, inverse edges hidden from the API) |
| Reverse closure, depth 6 | **~6 ms warm** (4.8–7.0 ms over 6 runs; ~25 ms cold) |
| Exposed services | **7 / 20 — 35%**, 3 of them tier-1 |
| Also exposed | 24 packages, 4 of 10 lockfiles |
| Infection paths returned | **82**, longest 7 hops |
| Sister packages in the 48h window | **3** of `dev_alex`'s 5 |
| Seed time | 502 nodes + 3,877 edges in **0.31 s**, 35 statements |

Raising the depth slider to 8 finds **9** services rather than 7: two sit
deliberately at depth 7 and 8, so the traversal horizon is real, not cosmetic.

Reproduce it yourself with `make up && make seed && make verify`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  React + Vite dashboard  ·  react-force-graph-2d  :5173      │
│  threat radar · live force graph · inspector · patch diff    │
└───────────────────────────────┬──────────────────────────────┘
                                │  REST /api/*
┌───────────────────────────────┴──────────────────────────────┐
│  FastAPI  :8000                                              │
│  closure · maintainer risk · typosquats · blast radius · fix │
│  ── traversal in HydraDB, scoring in Python ──               │
└───────────────────────────────┬──────────────────────────────┘
                                │  OpenCypher over HTTP :8443
┌───────────────────────────────┴──────────────────────────────┐
│  HydraDB — object-store-native graph engine                  │
│  Package · Version · Maintainer · Service · Lockfile         │
│  DEPENDS_ON · DEPENDED_ON_BY · MAINTAINED_BY · MAINTAINS     │
│  RESOLVED_IN · TYPOSQUAT_OF                                  │
└──────────────────────────────────────────────────────────────┘
```

### Graph schema

**Nodes** — `Package` (name, ecosystem, is_compromised, risk_score,
downloads_weekly) · `Version` (semver, published_at, compromised_window) ·
`Maintainer` (username, email, key_fingerprint) · `Service` (name, repo_url,
criticality) · `Lockfile` (filename, commit_hash).

**Edges** — `DEPENDS_ON` (constraint, is_dev, transitive_depth) ·
`MAINTAINED_BY` (since) · `RESOLVED_IN` (resolved_version) · `TYPOSQUAT_OF`
(edit_distance, similarity_score).

### The one design decision that matters

HydraDB runs variable-length traversal **only forward from a fixed source id**:

```
MATCH (b)-[:DEPENDS_ON*1..3]->(a {id:4})
→ "variable-length MATCH requires a fixed source id"
```

Radix's core question is the *reverse* closure — who depends on the compromised
package — which is precisely the rejected shape. So the seeder **materialises an
inverse edge**: every `DEPENDS_ON` is mirrored by a `DEPENDED_ON_BY`, and the
reverse question becomes a forward traversal the engine executes natively:

```cypher
MATCH (victim {id: $pkg})-[:DEPENDED_ON_BY*1..6]->(dependent)
RETURN DISTINCT dependent.id AS id
```

Costs 2× edge storage; turns an impossible query into a fast one. The same trick
mirrors `MAINTAINED_BY` into `MAINTAINS` for the maintainer walk.

Every engine constraint Radix was built against was verified by live probe and is
documented in **[docs/HYDRADB_CONTRACT.md](docs/HYDRADB_CONTRACT.md)** — including
the boot requirements that are easy to get wrong (`user: "0:0"`,
`RUST_MIN_STACK`, mounting volumes *at* the data paths) and the exact
`UNWIND` batch-write forms the engine accepts.

---

## Quickstart

**Requirements:** Docker, Python 3.12+, Node 18+.

```bash
git clone <this-repo> && cd Radix
make dev
```

That single target boots HydraDB, waits for `/readyz`, seeds the ecosystem graph,
and starts both the API and the dashboard. Then open **http://localhost:5173**.

Step by step, if you prefer:

```bash
make up        # docker compose up -d, wait for HydraDB /readyz
make seed      # load the ecosystem graph (idempotent)
make backend   # FastAPI on :8000
make frontend  # Vite dev server on :5173
make verify    # end-to-end check
```

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | liveness + HydraDB readiness |
| `GET /api/graph/full` | whole graph for the visualiser |
| `GET /api/closure/{package_id}` | transitive reverse closure |
| `POST /api/simulate-breach` | full incident: closure + maintainer + typosquat + radius |
| `GET /api/maintainer-risk/{id}` | sister packages by shared signing key |
| `GET /api/typosquats/{id}` | edit-distance neighbours |
| `POST /api/generate-fix` | lockfile patch + PR diff |

Full request/response shapes: **[docs/API_CONTRACT.md](docs/API_CONTRACT.md)**.

---

## Demo script (3 minutes)

**0:00 — The setup.** Dashboard open, 502-node ecosystem breathing on screen.
Threat radar reads total nodes, tracked lockfiles, zero active alerts.
*"This is an npm ecosystem: packages, the maintainers who sign them, and the
services that ship them."*

**0:25 — The problem.** *"`tanstack-query` just shipped a malicious 4.28.0. A
vector database can tell me which packages look similar. It cannot tell me which
of my services are actually exposed — that's a graph reachability question."*

**0:45 — Detonate.** Hit **Simulate TanStack Worm Attack**. The root flashes red;
infection pulses outward hop by hop along real transitive edges into services.
Latency counter lands in single-digit milliseconds.
*"That's a six-hop reverse closure across the whole enterprise graph."*

**1:15 — Blast radius.** Gauge sweeps to the exact exposed-services percentage.
*"Not a ranked guess — the exact set. These services, these lockfiles."*

**1:35 — The second wave.** Open the maintainer panel. *"The same stolen signing
key published sister packages inside the 48-hour window. Nobody has flagged
these yet. Per-package scanners cannot see this, because the connection isn't in
the package — it's in the graph."*

**2:05 — Typosquats.** Proximity radar with real edit distances.

**2:20 — Fix it.** Click a compromised node → **Generate Patch PR** → unified diff
pinning the last clean version, with npm overrides.
*"Detection to remediation without leaving the graph."*

**2:45 — Close.** *"Radix does traversal in HydraDB and scoring in Python.
Isolating a compromise is a reachability problem, and that's what a graph engine
is for."*

---

## Project layout

```
backend/app/    schema.py · hydra_client.py · analytics.py · remediation.py · main.py
scripts/        seed_ecosystem.py
frontend/src/   components/ · hooks/ · lib/
docs/           HYDRADB_CONTRACT.md · API_CONTRACT.md
docker-compose.yml · Makefile
```

## Attribution

Radix is built on open source and public data services:

- **[HydraDB](https://github.com/hydra-db/hydradb)** - the graph engine every traversal runs on (Apache-2.0 per its repository)
- **[OSV.dev](https://osv.dev)** - vulnerability and malicious-package advisories, including the OpenSSF malicious-packages corpus
- **npm registry** (registry.npmjs.org, api.npmjs.org) - package metadata, publish timestamps and download counts
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[uvicorn](https://www.uvicorn.org/)**, **[pydantic](https://docs.pydantic.dev/)**, **[requests](https://requests.readthedocs.io/)**, **[PyYAML](https://pyyaml.org/)**, **[pytest](https://pytest.org)** - backend stack
- **[React](https://react.dev/)**, **[Vite](https://vite.dev/)**, **[Tailwind CSS](https://tailwindcss.com/)**, **[react-force-graph](https://github.com/vasturiano/react-force-graph)**, **[d3-force](https://github.com/d3/d3-force)** - frontend stack
- **[node-semver](https://github.com/npm/node-semver)** - used as the test oracle for the pure-Python semver engine (8,806 cross-checked cases)

The TanStack worm scenario in the demo namespace is fictional, modelled on the
real May 2026 TanStack npm compromise; the live namespace's findings come
unmodified from OSV and the npm registry.

## License

MIT.
