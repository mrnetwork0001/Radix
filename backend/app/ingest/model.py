"""Intermediate representation for real-world ingestion.

Every ingestion source (npm/yarn/pnpm lockfiles, the npm registry, OSV)
normalises into these dataclasses, and the graph writer consumes *only* these.
That keeps parsers, network clients and graph writes independently testable,
and means adding an ecosystem later (PyPI) is a new parser, not a new pipeline.

Nothing in this module touches the network or HydraDB.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --- Lockfile layer --------------------------------------------------------


@dataclass(frozen=True)
class PackageRelease:
    """One resolved package@version pinned by a lockfile."""

    name: str                    # npm name, scoped names included ("@scope/pkg")
    version: str                 # exact resolved semver, e.g. "4.28.0"
    ecosystem: str = "npm"
    dev: bool = False            # true when only reachable via devDependencies
    direct: bool = False         # true when the root manifest names it directly
    resolved_url: str | None = None
    integrity: str | None = None


@dataclass(frozen=True)
class DepEdge:
    """A dependency requirement between two resolved releases.

    ``src_name is None`` means the edge originates at the root manifest
    (the service itself) rather than at another package.
    """

    src_name: str | None
    src_version: str | None
    dst_name: str
    constraint: str              # the semver range as written, e.g. "^4.0.0"
    dst_version: str             # what the lockfile actually resolved it to
    dev: bool = False


@dataclass
class ParsedLockfile:
    path: str                    # repo-relative path, e.g. "frontend/package-lock.json"
    kind: str                    # "npm" | "yarn" | "pnpm"
    root_name: str               # the manifest's "name", or the repo name
    releases: list[PackageRelease] = field(default_factory=list)
    edges: list[DepEdge] = field(default_factory=list)


@dataclass
class RepoScan:
    """Everything ingestion learned from one repository checkout."""

    source: str                  # what the user asked for (path or git URL)
    repo_name: str               # "org/repo" for URLs, directory name for paths
    repo_url: str | None         # normalised https URL when known
    commit_hash: str | None      # HEAD at scan time, when it is a git checkout
    lockfiles: list[ParsedLockfile] = field(default_factory=list)


# --- Registry layer --------------------------------------------------------


@dataclass
class MaintainerInfo:
    username: str
    email: str | None = None


@dataclass
class PackageMeta:
    """Registry metadata for one package, already trimmed to what Radix stores."""

    name: str
    ecosystem: str = "npm"
    maintainers: list[MaintainerInfo] = field(default_factory=list)
    published_at: dict[str, str] = field(default_factory=dict)  # version -> ISO 8601
    downloads_weekly: int | None = None
    latest: str | None = None


# --- Advisory layer --------------------------------------------------------


@dataclass
class Advisory:
    """One OSV advisory, trimmed to what the graph needs.

    ``malicious`` is true for OSV ``MAL-`` records (the OpenSSF
    malicious-packages corpus) - those set ``is_compromised`` on the Package.
    Ordinary vulnerabilities only contribute to ``risk_score``.
    """

    id: str                      # "MAL-2023-...", "GHSA-...", "CVE-..."
    package: str
    ecosystem: str = "npm"
    summary: str | None = None
    severity: str | None = None  # OSV database_specific severity when present
    malicious: bool = False
    published: str | None = None # ISO 8601
    modified: str | None = None
    affected_versions: list[str] = field(default_factory=list)   # exact versions, when enumerated
    affected_ranges: list[tuple[str | None, str | None]] = field(default_factory=list)
    # (introduced, fixed) pairs from OSV SEMVER events; None = unbounded side.
    aliases: list[str] = field(default_factory=list)


# --- Writer layer ----------------------------------------------------------


@dataclass
class IngestReport:
    """What one ingest run actually wrote, for the CLI to print and tests to assert."""

    namespace: str
    services: int = 0
    lockfiles: int = 0
    packages: int = 0
    versions: int = 0
    maintainers: int = 0
    depends_on: int = 0
    resolved_in: int = 0
    maintained_by: int = 0
    typosquats: int = 0
    compromised_marked: int = 0
    statements: int = 0
    wire_seconds: float = 0.0
