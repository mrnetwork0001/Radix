"""Version-exact refinement of the package-level reverse closure.

``HydraClient.get_transitive_closure`` walks ``DEPENDED_ON_BY`` and reports
everything package-reachable, which over-approximates: a dependent whose range
can never resolve the compromised release is not actually exposed. This module
prunes that over-approximation using the semver evidence already stored on the
graph, and records exactly what it did in the frozen ``precision`` object
(``ClosurePrecision`` in ``frontend/src/lib/types.ts``).

Semantics, in evidence-strength order:

1. **Hop 1 is gated.** Infection transmits across a direct ``DEPENDS_ON`` edge
   into the root only when that dependent's constraint can resolve
   ``bad_version`` (``semver_npm.satisfies``). A missing or unparseable
   constraint FAILS OPEN - the dependent stays exposed - because a security
   tool must never silently narrow the blast radius on bad data.
2. **Beyond hop 1 exposure is by inclusion.** No further gating.
3. **Reachability is recomputed client-side**: one unlabelled
   ``DEPENDED_ON_BY`` edge-list read (contract section 5 - endpoints on reads
   are unlabelled so one query spans every label pairing), then a BFS from the
   root that skips pruned hop-1 *edges*, not nodes - a dependent that is also
   reachable through a surviving route stays exposed. The result is
   intersected with the original affected sets, never grown, and the original
   traversal depth bound still applies.
4. **Lockfile pins override, strongest evidence wins.** A Lockfile whose
   ``RESOLVED_IN`` edge resolves the compromised package to a release outside
   the compromise window is not exposed even if package-reachable, and a
   service whose only exposure ran through such a clean lockfile is pruned
   with it. A lockfile resolving a windowed release stays exposed - among a
   lockfile's own pins the windowed one wins, so a mixed pin is never treated
   as clean. Windowed releases come from the package's Version nodes
   (``compromised_window``), joined on the Version's ``package_id`` property -
   never on ``name``, which is ``pkg@semver`` and would match nothing.

``semver_npm`` is imported lazily; if it (or its range parser) is unavailable
the whole refinement fails open to ``mode: "package"`` and prunes nothing.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Mapping

from . import schema

__all__ = ["refine"]


# --------------------------------------------------------------------------
# semver evidence helpers - all fail open
# --------------------------------------------------------------------------


def _load_semver():
    """The lazy import. ``None`` means: no evidence, prune nothing."""
    try:
        from . import semver_npm
    except Exception:  # pragma: no cover - only on a broken install
        return None
    return semver_npm if callable(getattr(semver_npm, "satisfies", None)) else None


def _version_is_valid(semver_mod: Any, version: str) -> bool:
    """A ``bad_version`` that does not parse must not prune anything at all:
    ``satisfies`` would answer False for *every* constraint and silently empty
    the closure. Prefer the module's own parser; fall back to the public-API
    identity check (every valid version satisfies its own equality range)."""
    parse = getattr(semver_mod, "_parse_version", None)
    if callable(parse):
        try:
            return parse(version) is not None
        except Exception:
            return False
    try:
        return bool(semver_mod.satisfies(version, "=" + version))
    except Exception:
        return False


def _range_is_valid(semver_mod: Any, constraint: Any) -> bool:
    """True only when the constraint *provably* parses as an npm range.

    ``satisfies`` alone cannot distinguish "valid range that excludes the
    version" from garbage, so validity comes from the parser. If the parser
    is unavailable nothing is provably valid and nothing gets pruned - the
    fail-open direction.
    """
    if not isinstance(constraint, str) or not constraint.strip():
        return False
    parse = getattr(semver_mod, "_parse_range", None)
    if not callable(parse):
        return False
    try:
        return parse(constraint) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# Local pieces
# --------------------------------------------------------------------------

_LABEL_ID_KEYS = (
    "affected_package_ids",
    "affected_version_ids",
    "affected_maintainer_ids",
    "affected_service_ids",
    "affected_lockfile_ids",
)

_TIER1 = frozenset({"tier-1", "tier1", "tier_1", "critical"})


def _latency_of(result: Any) -> float:
    return float(getattr(result, "latency_ms", 0.0) or 0.0)


def _package_level(closure: Mapping[str, Any]) -> dict[str, Any]:
    """The pre-pruning numbers the UI compares against."""
    blast = closure.get("blast_radius") or {}
    exposed = int(blast.get("exposed_services", len(closure.get("affected_service_ids") or [])))
    percentage = blast.get("percentage")
    if percentage is None:
        total = int(blast.get("total_services", 0) or 0)
        percentage = round(exposed / total * 100.0, 1) if total else 0.0
    return {"exposed_services": exposed, "percentage": float(percentage)}


def _bfs(
    adjacency: Mapping[int, list[int]],
    root_id: int,
    depth: int,
    hop1_pruned: frozenset[int] | set[int],
    blocked: frozenset[int] | set[int],
) -> set[int]:
    """Depth-bounded BFS over ``DEPENDED_ON_BY``.

    ``hop1_pruned`` removes the root->X *edge* only; X is still discoverable
    through a surviving dependent. ``blocked`` nodes (clean-pinned lockfiles)
    are never entered, so nothing beyond them draws exposure through them.
    """
    distance: dict[int, int] = {root_id: 0}
    queue: deque[int] = deque([root_id])
    while queue:
        current = queue.popleft()
        hops = distance[current]
        if hops >= depth:
            continue
        for nxt in adjacency.get(current, ()):
            if nxt in distance or nxt in blocked:
                continue
            if current == root_id and nxt in hop1_pruned:
                continue
            distance[nxt] = hops + 1
            queue.append(nxt)
    reached = set(distance)
    reached.discard(root_id)
    return reached


def _recompute_blast(
    client: Any,
    previous: Mapping[str, Any],
    node_by_id: Mapping[int, Mapping[str, Any]],
    service_ids: Iterable[int],
    lockfile_ids: Iterable[int],
) -> tuple[dict[str, Any], float]:
    """``BlastRadius`` for the pruned sets - analytics helpers when importable,
    otherwise the same fields computed locally (the pruned sets only shrink, so
    every surviving service is already hydrated in ``node_by_id``)."""
    try:
        from . import analytics
    except Exception:  # pragma: no cover - analytics ships with this package
        analytics = None
    if analytics is not None:
        fleet = analytics.load_fleet(client)
        return analytics.compute_blast_radius(fleet, service_ids, lockfile_ids), fleet.latency_ms

    exposed = {int(sid) for sid in service_ids}
    total_services = int(previous.get("total_services", 0) or 0)
    percentage = (len(exposed) / total_services * 100.0) if total_services else 0.0
    tier1 = sum(
        1
        for sid in exposed
        if str((node_by_id.get(sid) or {}).get("criticality") or "").lower() in _TIER1
    )
    return {
        "exposed_services": len(exposed),
        "total_services": total_services,
        "percentage": round(percentage, 1),
        "exposed_lockfiles": len({int(lid) for lid in lockfile_ids}),
        "total_lockfiles": int(previous.get("total_lockfiles", 0) or 0),
        "tier1_exposed": tier1,
    }, 0.0


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def refine(client: Any, closure: dict, bad_version: str | None) -> dict:
    """Prune the package-level closure down to version-exact exposure.

    Mutates ``closure`` in place and returns it, with the ``affected_*`` lists
    pruned, ``paths`` filtered so the animation never crosses a pruned hop-1
    edge or a pruned node, ``blast_radius`` recomputed from the pruned sets,
    and the frozen ``precision`` object attached.

    ``bad_version`` of ``None`` (or an unparseable one, or a missing semver
    module) fails open to ``mode: "package"`` with nothing pruned.
    """
    package_level = _package_level(closure)

    def _package_mode() -> dict:
        closure["precision"] = {
            "mode": "package",
            "bad_version": bad_version,
            "excluded_direct": [],
            "pruned_package_ids": [],
            "pruned_service_ids": [],
            "pruned_lockfile_ids": [],
            "package_level": package_level,
        }
        return closure

    if bad_version is None:
        return _package_mode()
    semver_mod = _load_semver()
    if semver_mod is None or not _version_is_valid(semver_mod, bad_version):
        return _package_mode()

    root_id = int((closure.get("root") or {}).get("id"))
    try:
        depth = max(1, int(closure.get("depth") or 6))
    except (TypeError, ValueError):
        depth = 6
    latency = 0.0

    original_set: set[int] = {int(i) for i in closure.get("affected_ids") or []}
    for key in _LABEL_ID_KEYS:
        original_set.update(int(i) for i in closure.get(key) or [])

    # Hydrated nodes, captured before any pruning: names for excluded_direct
    # and criticality for the local blast-radius fallback.
    node_by_id: dict[int, Mapping[str, Any]] = {}
    for node in closure.get("affected_nodes") or []:
        if node.get("id") is not None:
            node_by_id[int(node["id"])] = node

    # -- 1. hop-1 constraints: one query per the engine subset (no IN) -------
    result = client.execute(
        f"MATCH (a)-[r:{schema.DEPENDS_ON}]->(b {{id: $pkg}}) "
        f"RETURN a.id AS id, r.constraint AS c",
        {"pkg": root_id},
    )
    latency += _latency_of(result)
    constraints_by_dependent: dict[int, list[Any]] = {}
    for row in result.rows:
        if row.get("id") is None:
            continue
        constraints_by_dependent.setdefault(int(row["id"]), []).append(row.get("c"))

    # A dependent is excluded only when EVERY edge it has into the root carries
    # a provably valid range that cannot resolve bad_version. Missing or
    # garbage constraints fail open and keep it exposed.
    excluded_hop1: dict[int, str] = {}
    for dep_id, constraints in constraints_by_dependent.items():
        if dep_id == root_id:
            continue
        first_excluding: str | None = None
        exposed = False
        for constraint in constraints:
            if not _range_is_valid(semver_mod, constraint) or semver_mod.satisfies(
                bad_version, constraint
            ):
                exposed = True
                break
            if first_excluding is None:
                first_excluding = constraint
        if not exposed and first_excluding is not None:
            excluded_hop1[dep_id] = first_excluding

    # -- 2. the root's Version nodes: the compromise window ------------------
    # Joined on package_id, NOT name (a Version is named `pkg@semver`).
    result = client.execute(
        f"MATCH (v:{schema.VERSION}) WHERE v.package_id = $pkg "
        f"RETURN v.id AS id, v.semver AS semver, v.compromised_window AS w",
        {"pkg": root_id},
    )
    latency += _latency_of(result)
    root_version_ids: set[int] = set()
    windowed_version_ids: set[int] = set()
    for row in result.rows:
        if row.get("id") is None:
            continue
        vid = int(row["id"])
        root_version_ids.add(vid)
        # The stored window flag is the authority; the bad version itself is
        # windowed by definition, belt-and-braces in the exposed direction.
        if bool(row.get("w")) or str(row.get("semver") or "") == bad_version:
            windowed_version_ids.add(vid)

    # -- 3. lockfile pin evidence: RESOLVED_IN, endpoints unlabelled ---------
    result = client.execute(
        f"MATCH (a)-[r:{schema.RESOLVED_IN}]->(b) RETURN a.id AS source, b.id AS target"
    )
    latency += _latency_of(result)
    pin_windowed: set[int] = set()
    pin_clean: set[int] = set()
    for row in result.rows:
        if row.get("source") is None or row.get("target") is None:
            continue
        src, dst = int(row["source"]), int(row["target"])
        if dst not in root_version_ids:
            continue  # a resolution of some other package
        if schema.label_for_id(src) != schema.LOCKFILE:
            continue  # the package's own release-history edges
        if dst in windowed_version_ids:
            pin_windowed.add(src)
        else:
            pin_clean.add(src)
    # Strongest evidence wins: among one lockfile's pins a windowed resolution
    # beats a clean one, so only an all-clean pin set earns the override. A
    # windowed pin keeps the lockfile exposed *if it is still reachable* - it
    # does not resurrect one whose hop-1 transmission was gated away.
    clean_lockfiles = pin_clean - pin_windowed

    # -- 4. reachability, recomputed from the routes already in hand ---------
    # The closure carries root-first SSpaths chains, which describe exactly the
    # reachable subgraph this BFS explores. Deriving adjacency from them costs
    # nothing, where scanning every DEPENDED_ON_BY edge to rebuild the same
    # subgraph was ~80% of this function's time and grew with the whole graph
    # rather than with the incident.
    adjacency: dict[int, list[int]] = {}
    evidenced: set[int] = set()
    for chain in closure.get("paths") or ():
        ids = [int(node) for node in chain]
        evidenced.update(ids)
        for source, target in zip(ids, ids[1:]):
            bucket = adjacency.setdefault(source, [])
            if target not in bucket:
                bucket.append(target)

    hop1_pruned = set(excluded_hop1)
    reachable = _bfs(adjacency, root_id, depth, hop1_pruned, clean_lockfiles)

    # Fail open. `pathCount` can truncate the chain set, so a node the paths
    # never mention has no evidence either way and keeps its exposure; only a
    # gated hop-1 dependent is pruned without needing a route to prove it.
    unevidenced = original_set - evidenced - hop1_pruned
    final_set = ((reachable | unevidenced) & original_set) - clean_lockfiles

    # -- 5. prune the affected lists in place, keeping their order -----------
    pruned_by_key: dict[str, list[int]] = {}
    for key in ("affected_ids",) + _LABEL_ID_KEYS:
        if key not in closure:
            continue
        kept: list[int] = []
        pruned: list[int] = []
        for value in closure[key]:
            (kept if int(value) in final_set else pruned).append(int(value))
        closure[key] = kept
        pruned_by_key[key] = pruned

    if "affected_nodes" in closure:
        closure["affected_nodes"] = [
            node
            for node in closure["affected_nodes"]
            if node.get("id") is not None and int(node["id"]) in final_set
        ]

    # Paths: drop any chain whose hop-1 edge was pruned - even when the hop-1
    # node survives via another route - and any chain visiting a pruned node,
    # so the animation cannot cross a pruned edge.
    if "paths" in closure:
        kept_paths = []
        for chain in closure["paths"]:
            ids = [int(n) for n in chain]
            if len(ids) >= 2 and ids[1] in hop1_pruned:
                continue
            if any(n not in final_set for n in ids[1:]):
                continue
            kept_paths.append(chain)
        closure["paths"] = kept_paths

    # -- 6. blast radius from the pruned sets --------------------------------
    blast, fleet_latency = _recompute_blast(
        client,
        closure.get("blast_radius") or {},
        node_by_id,
        closure.get("affected_service_ids") or [],
        closure.get("affected_lockfile_ids") or [],
    )
    latency += fleet_latency
    closure["blast_radius"] = blast

    # -- 7. the frozen precision object --------------------------------------
    closure["precision"] = {
        "mode": "version",
        "bad_version": bad_version,
        "excluded_direct": [
            {
                "id": dep_id,
                "name": str((node_by_id.get(dep_id) or {}).get("name") or f"node-{dep_id}"),
                "constraint": excluded_hop1[dep_id],
            }
            for dep_id in sorted(hop1_pruned)
        ],
        "pruned_package_ids": pruned_by_key.get("affected_package_ids", []),
        "pruned_service_ids": pruned_by_key.get("affected_service_ids", []),
        "pruned_lockfile_ids": pruned_by_key.get("affected_lockfile_ids", []),
        "package_level": package_level,
    }
    closure["latency_ms"] = round(float(closure.get("latency_ms") or 0.0) + latency, 3)
    return closure
