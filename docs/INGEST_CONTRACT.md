# Radix Ingestion Contract

Frozen interfaces for the "make it real" build: real lockfiles, real registry
metadata, real advisories. Every module codes against
`backend/app/ingest/model.py` (the IR) and this document. The graph writer is
the **only** module that talks to HydraDB; parsers and network clients are pure.

Pipeline:

```
repo.py ──► lockfiles.py ──► RepoScan ─┐
registry.py ─► PackageMeta per name ───┤──► graph_writer.py ──► HydraDB
osv.py ─────► Advisory list ───────────┘         (namespace-scoped)
```

## Module interfaces

### `backend/app/ingest/repo.py`
```python
def scan_source(target: str, workdir: Path | None = None) -> RepoScan
```
`target` is a local directory **or** a git URL (https / ssh). URLs are shallow-
cloned into `workdir` (default: a temp dir) and cleaned up. Local paths are
scanned in place, never modified. Records `commit_hash` when the target is a
git checkout. Skips `node_modules/` entirely.

### `backend/app/ingest/lockfiles.py`
```python
def discover_lockfiles(root: Path) -> list[Path]     # package-lock.json, yarn.lock, pnpm-lock.yaml
def parse_lockfile(path: Path, repo_root: Path) -> ParsedLockfile
```
Must handle: package-lock v2/v3 (the `packages` map - derive per-package
`DepEdge`s from each entry's `dependencies` field), v1 (nested `dependencies`),
yarn.lock v1 (the `name@range:` block format), pnpm-lock v6/v9 (YAML,
`importers` + `packages`). Root-manifest deps become edges with
`src_name=None`. `dev` flags propagate from the lockfile's own metadata.

### `backend/app/ingest/registry.py`
```python
class NpmRegistry:
    def __init__(self, cache_dir: Path | None = None, user_agent: str = "radix-ingest"): ...
    def get_meta(self, name: str) -> PackageMeta | None       # registry.npmjs.org/{name}
    def fill_downloads(self, metas: dict[str, PackageMeta]) -> None
```
- Full metadata doc (not the abbreviated corgi format - it lacks the `time`
  map, and `published_at` is the maintainer sentinel's whole feature).
- **Cache to disk** (`data/cache/registry/`) keyed by name, with ETag
  revalidation; a re-run must not re-download megabytes per package.
- Downloads via `api.npmjs.org/downloads/point/last-week/…`; the bulk
  comma-separated form takes up to 128 **unscoped** names per call, scoped
  names go one at a time with `@scope%2Fname` encoding.
- Be polite: small concurrency, retry with backoff on 429/5xx, never crash the
  pipeline on one bad package - return `None` and let the writer proceed.

### `backend/app/ingest/osv.py`
```python
class OsvClient:
    def query_batch(self, packages: Iterable[tuple[str, str]]) -> dict[str, list[str]]
        # (ecosystem, name) -> [vuln ids]; POST api.osv.dev/v1/querybatch, chunks of ≤1000
    def get_advisory(self, vuln_id: str) -> Advisory | None   # GET /v1/vulns/{id}, disk-cached
    def advisories_for(self, packages: Iterable[tuple[str, str]]) -> list[Advisory]
```
`Advisory.malicious` is true for `MAL-` ids. Map OSV `affected[].ranges[]`
SEMVER events to `(introduced, fixed)` pairs and `affected[].versions` to
`affected_versions`. Keep `severity` from `database_specific.severity` when
present.

### `backend/app/ingest/graph_writer.py`
```python
class GraphWriter:
    def __init__(self, client: HydraClient): ...
    def ingest(self, scan: RepoScan, meta: dict[str, PackageMeta],
               advisories: list[Advisory]) -> IngestReport
    def apply_advisories(self, advisories: list[Advisory]) -> int   # the watcher's path
    def known_packages(self) -> dict[str, int]                      # name -> vertex id
```

**Node identity - the graph is the id registry.** No state files. At init,
label-scan the namespace for existing `name -> id` per label and allocate new
ids sequentially after the highest existing offset in each label's partition
(see `schema._BASES`). Re-ingesting must MERGE onto the same ids.

**Edge identity - deterministic hashes.** Batched relationship writes need an
explicit id (contract §4). Derive it as
`int.from_bytes(sha1(f"{edge_type}|{src_id}|{dst_id}").digest()[:8]) >> 2`
(62-bit) so re-ingestion MERGEs the same edge instead of duplicating it.
**Probe first** that HydraDB round-trips integers this large as `{id: ...}`
relationship properties before assuming.

Same write forms as the seeder (contract §4): `MERGE`-by-id + `SET` for nodes
grouped by property signature, `MERGE (s)-[r {id: row.eid}]->(d) SET …` for
edges, one label per endpoint, chunked UNWIND, and every `DEPENDS_ON` /
`MAINTAINED_BY` mirrored as `DEPENDED_ON_BY` / `MAINTAINS`.

Advisory application: `MAL-` advisory ⇒ Package `is_compromised = true`; any
advisory whose ranges/versions include a stored Version ⇒ that Version
`compromised_window = true`. Store the advisory ids on the Package as a
comma-joined string property `advisories` (property values are scalars only).
Risk scores: bump per non-malicious advisory, capped; `1.0` implies malicious.

Typosquat pass: Levenshtein ≤ 2 (plus homoglyph normalisation) between every
ingested package name and the namespace's high-download names, written as
`TYPOSQUAT_OF` edges. Skip pairs that share a scope prefix - `@types/react` is
not squatting `react`.

### `backend/app/semver_npm.py`
```python
def satisfies(version: str, range_: str) -> bool
def max_satisfying(versions: Iterable[str], range_: str) -> str | None
```
npm range semantics in pure Python: `^ ~ >= > <= < =`, `x`/`*` wildcards,
hyphen ranges, `||` alternatives, whitespace-AND comparator sets, prerelease
ordering per SemVer §11 and npm's rule that prereleases only satisfy ranges
that mention a prerelease of the *same* `[major, minor, patch]` tuple. This is
what upgrades the closure from package-level to version-level: a dependent
whose constraint cannot resolve the compromised version is not actually
exposed.

### `sentinel/watcher.py` - the 24/7 agent
```python
python -m sentinel.watcher --interval 900 [--once] [--dry-run]
```
Loop: read every Package name in the namespace → `OsvClient.advisories_for` →
`GraphWriter.apply_advisories` → log a one-line delta (`+2 compromised,
3 versions windowed`). State lives in the graph; the process itself is
stateless and safe to restart. Config via env: `HYDRA_HTTP_URL`, `HYDRA_TOKEN`,
`HYDRA_NAMESPACE`, `SENTINEL_INTERVAL`, `SENTINEL_REPOS` (optional
comma-separated re-scan list).

## Namespace strategy

`X-Graph-Namespace` isolates graphs inside one HydraDB. `HydraClient` already
reads `HYDRA_NAMESPACE` (default `radix`).

**Authorization is prefix-scoped to the node's boot namespace** (verified by
probe): with the dev container booted as `GRAPH_NAMESPACE=radix`, the token
authorizes `radix` and `radix/...` sub-scopes only - `radix-test` and
`radix-live` are rejected with `permission_denied`. Convention:

| namespace | contents |
|---|---|
| `radix` | the seeded demo world (untouched by ingestion tests) |
| `radix/live` | real ingested data, local dev |
| `radix/test` | scratch for automated tests - safe to wipe (`RADIX_TEST_NAMESPACE` overrides) |
| `radix-live` | production only - the prod container *boots* with this namespace, which is what authorizes it there |

**Never `DETACH DELETE` in `radix` from ingestion code or tests.**

## What "verified" means here

Every module is verified against the real thing before it is done: parse
`frontend/package-lock.json` from this repo (it is real), fetch real metadata
for real packages, run a real OSV query for a package with a famous advisory
(`event-stream` has both GHSA and MAL records), write to `radix-test` and read
it back. No fixtures standing in for the network on first verification -
fixtures are for regression, not for proof.
