"""Real-world ingestion for Radix: the IR plus the pure parser entry points.

Network clients (``registry``, ``osv``) and the graph writer live in their own
modules inside this package; everything they exchange is the IR re-exported
here, per ``docs/INGEST_CONTRACT.md``.
"""

from .lockfiles import discover_lockfiles, parse_lockfile
from .model import (
    Advisory,
    DepEdge,
    IngestReport,
    MaintainerInfo,
    PackageMeta,
    PackageRelease,
    ParsedLockfile,
    RepoScan,
)
from .repo import scan_source

__all__ = [
    "Advisory",
    "DepEdge",
    "IngestReport",
    "MaintainerInfo",
    "PackageMeta",
    "PackageRelease",
    "ParsedLockfile",
    "RepoScan",
    "discover_lockfiles",
    "parse_lockfile",
    "scan_source",
]
