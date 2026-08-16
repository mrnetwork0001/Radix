#!/usr/bin/env python3
"""Ingest real repositories into Radix.

    python scripts/ingest.py <path-or-git-url> [...] [--namespace radix/live]

Pipeline per target: scan lockfiles -> enrich from the npm registry -> pull OSV
advisories -> write the graph. Everything is idempotent, so re-running after a
new commit or a new advisory only writes the delta.

The demo world lives in namespace ``radix``; real data defaults to the
``radix/live`` sub-scope so the two never mix. Point the backend at real data
with ``HYDRA_NAMESPACE=radix/live``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.hydra_client import HydraClient  # noqa: E402
from app.ingest.lockfiles import discover_lockfiles  # noqa: E402  (re-exported check)
from app.ingest.repo import scan_source  # noqa: E402
from app.ingest.registry import NpmRegistry  # noqa: E402
from app.ingest.osv import OsvClient  # noqa: E402
from app.ingest.graph_writer import GraphWriter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest real repositories into Radix")
    parser.add_argument("targets", nargs="+", help="local paths and/or git URLs")
    parser.add_argument(
        "--namespace",
        default=os.environ.get("HYDRA_NAMESPACE_LIVE", "radix/live"),
        help="HydraDB namespace to write (default: radix/live). Token authz is "
        "prefix-scoped to the node's boot namespace, so local sub-scopes must be "
        "'radix/<x>'; the demo world in plain 'radix' is deliberately not the default",
    )
    parser.add_argument("--no-registry", action="store_true", help="skip npm registry enrichment")
    parser.add_argument("--no-osv", action="store_true", help="skip the OSV advisory sync")
    parser.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "data" / "cache")
    args = parser.parse_args()

    if args.namespace == "radix":
        # The seeded demo graph and real data must never mix: demo nodes carry
        # fabricated compromise flags that would corrupt real findings.
        print("refusing to ingest into the demo namespace 'radix'; use --namespace", file=sys.stderr)
        return 2

    client = HydraClient(namespace=args.namespace)
    writer = GraphWriter(client)
    registry = None if args.no_registry else NpmRegistry(cache_dir=args.cache_dir / "registry")
    osv = None if args.no_osv else OsvClient(cache_dir=args.cache_dir / "osv")

    t0 = time.perf_counter()
    for target in args.targets:
        print(f"── scanning {target}")
        scan = scan_source(target)
        if not scan.lockfiles:
            print("   no lockfiles found - nothing to ingest")
            continue
        for lf in scan.lockfiles:
            print(f"   {lf.path}: {len(lf.releases)} releases, {len(lf.edges)} edges ({lf.kind})")

        names = sorted({r.name for lf in scan.lockfiles for r in lf.releases})

        metas = {}
        if registry is not None:
            print(f"   registry: enriching {len(names)} packages…")
            for name in names:
                meta = registry.get_meta(name)
                if meta is not None:
                    metas[name] = meta
            registry.fill_downloads(metas)
            print(f"   registry: {len(metas)}/{len(names)} enriched")

        advisories = []
        if osv is not None:
            print(f"   osv: querying {len(names)} packages…")
            advisories = osv.advisories_for([("npm", n) for n in names])
            malicious = sum(1 for a in advisories if a.malicious)
            print(f"   osv: {len(advisories)} advisories ({malicious} malicious)")

        report = writer.ingest(scan, metas, advisories)
        print(
            f"   wrote: {report.packages} packages, {report.versions} versions, "
            f"{report.maintainers} maintainers, {report.services} services, "
            f"{report.lockfiles} lockfiles | {report.depends_on} DEPENDS_ON, "
            f"{report.resolved_in} RESOLVED_IN, {report.typosquats} typosquats | "
            f"{report.compromised_marked} compromised | "
            f"{report.statements} statements, {report.wire_seconds:.2f}s on the wire"
        )

    print(f"── done in {time.perf_counter() - t0:.1f}s (namespace: {args.namespace})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
