#!/usr/bin/env python3
"""Verify every number quoted in README.md against a running Radix.

A README drifts the moment the product moves. This turns the measured-results
table from a promise into an assertion: each entry in ``claims.json`` names a
value, where it is quoted, and what it must equal, and this script fetches the
live answers and compares. A stale claim exits non-zero.

    python scripts/check_claims.py                       # against the deployment
    python scripts/check_claims.py --target http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 60


def _get(url: str, payload: dict | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read())


def collect(target: str, breach: dict) -> dict[str, Any]:
    """One pass over the live API, reduced to the values the claims reference."""
    graph = _get(f"{target}/api/graph/full")
    stats = graph["stats"]

    incident = _get(f"{target}/api/simulate-breach", breach)
    closure = incident["closure"]
    blast = incident["blast_radius"]
    precision = closure.get("precision") or {}

    deeper = _get(f"{target}/api/simulate-breach", {**breach, "depth": 8})

    fix = _get(
        f"{target}/api/generate-fix",
        {"package_id": incident["root"]["id"], "bad_version": incident["compromised_version"]},
    )

    sisters = (incident.get("maintainer_risk") or {}).get("sister_packages") or []

    return {
        "graph.nodes": len(graph["nodes"]),
        "graph.edges_api": len(graph["edges"]),
        "graph.packages": stats["packages"],
        "graph.services": stats["services"],
        "graph.lockfiles": stats["lockfiles"],
        "blast.exposed_services": blast["exposed_services"],
        "blast.percentage": blast["percentage"],
        "blast.tier1": blast["tier1_exposed"],
        "blast.exposed_lockfiles": blast["exposed_lockfiles"],
        "closure.packages": len(closure["affected_package_ids"]),
        "closure.paths": len(closure["paths"]),
        "closure.longest_path": max((len(p) for p in closure["paths"]), default=0),
        "closure.latency_ms": closure["latency_ms"],
        "precision.mode": precision.get("mode"),
        "precision.pruned": len(precision.get("pruned_package_ids") or []),
        "precision.excluded_names": sorted(
            e["name"] for e in (precision.get("excluded_direct") or [])
        ),
        "depth8.exposed_services": deeper["blast_radius"]["exposed_services"],
        "maintainer.sisters_in_window": sum(
            1 for s in sisters if s.get("published_within_window")
        ),
        "typosquats.count": len(incident.get("typosquats") or []),
        "fix.safe_version": fix.get("safe_version"),
        "fix.patched_services": len(fix.get("patches") or []),
    }


def compare(expected: Any, actual: Any, op: str) -> bool:
    if actual is None:
        return False
    if op == "eq":
        return actual == expected
    if op == "lte":
        return actual <= expected
    if op == "gte":
        return actual >= expected
    raise ValueError(f"unknown op {op!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=REPO_ROOT / "claims.json")
    parser.add_argument("--target", help="overrides the target in claims.json")
    args = parser.parse_args()

    manifest = json.loads(args.claims.read_text())
    target = (args.target or manifest["target"]).rstrip("/")

    print(f"checking {len(manifest['claims'])} claims against {target}\n")
    try:
        actual = collect(target, manifest["breach"])
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"FAIL  cannot reach {target}: {error}", file=sys.stderr)
        return 2

    failures = 0
    for claim in manifest["claims"]:
        key, op = claim["id"], claim.get("op", "eq")
        got = actual.get(key)
        ok = compare(claim["expect"], got, op)
        failures += not ok
        symbol = "ok  " if ok else "FAIL"
        rel = "" if op == "eq" else f"{op} "
        print(f"  {symbol} {key:32s} {rel}{claim['expect']!r}" + ("" if ok else f"   got {got!r}"))
        if not ok:
            print(f"       quoted in: {claim['where']}")

    print()
    if failures:
        print(f"{failures} of {len(manifest['claims'])} claims are STALE - fix the README or the product")
        return 1
    print(f"all {len(manifest['claims'])} claims verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
