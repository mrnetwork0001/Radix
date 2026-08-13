"""Scoring, exposure, and timeline analytics for Radix.

HydraDB does the traversal; this module does the arithmetic. That split is
forced by the engine rather than chosen (``docs/HYDRADB_CONTRACT.md`` §5):

* ``WITH`` is pass-through only, so there are no multi-stage aggregation
  pipelines — anything that needs a second reduction step lands here.
* There is no ``min``/``max``, so every ranking is Python's job.
* There is no ``IN``, so "criticality for *these* service ids" cannot be asked
  server-side; one label scan is joined against the exposed set locally.

Every function that touches HydraDB returns the wire latency alongside its
result, because ``latency_ms`` in the API contract must cover the traversal only
and never FastAPI's own serialisation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import schema
from .hydra_client import HydraClient

__all__ = [
    "Fleet",
    "RISK_WEIGHTS",
    "blast_radius",
    "build_timeline",
    "composite_risk_score",
    "compute_blast_radius",
    "graph_node",
    "load_fleet",
    "maintainer_risk",
    "package_risk_profile",
    "primary_maintainer_id",
    "typosquats",
]


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

#: Criticality strings that count as tier-1. The seeder writes ``tier-1``; the
#: synonyms keep the tally correct if another ingest spells it differently.
TIER1_CRITICALITY = frozenset({"tier-1", "tier1", "tier_1", "critical"})


def graph_node(node_id: int, properties: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble an API-contract ``GraphNode`` from a projected row.

    The label is recovered from the partitioned integer id space rather than
    from the row, which is what lets a bare ``vertex_id`` coming out of a
    variable-length ``MATCH`` be classified without a second round trip.
    Properties that a label does not carry arrive as ``None`` and are dropped —
    the contract says type-specific fields are omitted, not nulled.
    """
    node_id = int(node_id)
    node: dict[str, Any] = {"id": node_id, "label": schema.label_for_id(node_id)}
    node.update({key: value for key, value in properties.items() if value is not None})
    node.setdefault("name", f"node-{node_id}")
    return node


def _parse_iso(value: Any) -> datetime | None:
    """Parse the seeder's ``...Z`` timestamps; ``None`` for anything unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


# --------------------------------------------------------------------------
# Blast radius
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fleet:
    """One snapshot of the deployment fleet: every Service, plus lockfile total.

    Read once per request and reused by the blast radius, the timeline, and the
    fix generator, so a simulate-breach call pays for the Service scan once
    instead of three times.
    """

    services: tuple[dict[str, Any], ...]
    total_lockfiles: int
    latency_ms: float

    @property
    def total_services(self) -> int:
        return len(self.services)

    def by_id(self) -> dict[int, dict[str, Any]]:
        return {service["id"]: service for service in self.services}

    def names(self, service_ids: Iterable[int]) -> list[str]:
        index = self.by_id()
        wanted = {int(sid) for sid in service_ids}
        return sorted(
            str(index[sid].get("name") or f"service-{sid}") for sid in wanted if sid in index
        )

    def tier1_ids(self) -> set[int]:
        return {
            service["id"]
            for service in self.services
            if str(service.get("criticality") or "").lower() in TIER1_CRITICALITY
        }


def load_fleet(client: HydraClient) -> Fleet:
    """Scan every Service and count the Lockfiles. Two statements, no joins."""
    services_result = client.execute(
        f"MATCH (s:{schema.SERVICE}) RETURN s.id AS id, s.name AS name, "
        f"s.criticality AS criticality, s.repo_url AS repo_url, s.team AS team, "
        f"s.deploy_env AS deploy_env"
    )
    lockfiles_result = client.execute(f"MATCH (n:{schema.LOCKFILE}) RETURN count(*) AS c")

    services = tuple(
        graph_node(
            row["id"],
            {
                "name": row.get("name"),
                "criticality": row.get("criticality"),
                "repo_url": row.get("repo_url"),
                "team": row.get("team"),
                "deploy_env": row.get("deploy_env"),
            },
        )
        for row in services_result.rows
        if row.get("id") is not None
    )
    return Fleet(
        services=services,
        total_lockfiles=int(lockfiles_result.scalar("c", 0) or 0),
        latency_ms=services_result.latency_ms + lockfiles_result.latency_ms,
    )


def compute_blast_radius(
    fleet: Fleet,
    exposed_service_ids: Iterable[int],
    exposed_lockfile_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """The contract's ``BlastRadius`` object. Pure — no I/O.

    ``percentage`` is ``exposed_services / total_services * 100``; tier-1 is
    counted by intersecting the exposed set with the fleet's tier-1 ids.
    """
    exposed_services = {int(sid) for sid in exposed_service_ids or ()}
    exposed_lockfiles = {int(lid) for lid in exposed_lockfile_ids or ()}
    total_services = fleet.total_services

    percentage = (len(exposed_services) / total_services * 100.0) if total_services else 0.0
    return {
        "exposed_services": len(exposed_services),
        "total_services": total_services,
        "percentage": round(percentage, 1),
        "exposed_lockfiles": len(exposed_lockfiles),
        "total_lockfiles": fleet.total_lockfiles,
        "tier1_exposed": len(exposed_services & fleet.tier1_ids()),
    }


def blast_radius(
    client: HydraClient,
    exposed_service_ids: Iterable[int],
    exposed_lockfile_ids: Iterable[int] | None = None,
) -> tuple[dict[str, Any], float]:
    """``(BlastRadius, latency_ms)`` for callers that have no Fleet in hand."""
    fleet = load_fleet(client)
    return compute_blast_radius(fleet, exposed_service_ids, exposed_lockfile_ids), fleet.latency_ms


# --------------------------------------------------------------------------
# Composite risk score
# --------------------------------------------------------------------------

#: Weighting of the four independent risk signals. Each component is normalised
#: to 0.0-1.0 first, so the weights below are the whole story:
#:
#:   reach      0.40  How far a compromise propagates. This is the dominant term
#:                    because reach is the entire point of a supply-chain attack:
#:                    a package nobody depends on cannot hurt anybody. Scaled
#:                    logarithmically — 1000 dependents is worse than 10, but not
#:                    100x worse, and the curve saturates at REACH_SATURATION.
#:   window     0.25  Whether a release actually landed inside the compromise
#:                    window. This separates "could be attacked" from "was
#:                    attacked", so it is the largest of the contextual signals.
#:   maintainer 0.20  Fraction of the maintainer's *other* packages that also
#:                    published inside the window. An account takeover is a
#:                    shared blast surface; a maintainer who shipped four
#:                    packages in the window is a different problem from one who
#:                    shipped one.
#:   typosquat  0.15  Proximity of the nearest impersonating package. Smallest
#:                    weight because a typosquat is a *parallel* attack path
#:                    rather than exposure of this package's own dependents.
#:
#: The three contextual signals sum to 0.60, so a fully-contextualised package
#: with almost no reach still clears the "high" band (0.50) — deliberate, since
#: a freshly-published malicious package has no dependents yet.
RISK_WEIGHTS: dict[str, float] = {
    "reach": 0.40,
    "window": 0.25,
    "maintainer": 0.20,
    "typosquat": 0.15,
}

#: Dependent count at which the reach term reaches 1.0. Sized to this graph's
#: fleet (~500 nodes); a real registry ingest would raise it.
REACH_SATURATION = 60

_RISK_BANDS = ((0.75, "critical"), (0.50, "high"), (0.25, "moderate"), (0.0, "low"))


def _band(score: float) -> str:
    for threshold, name in _RISK_BANDS:
        if score >= threshold:
            return name
    return "low"


def composite_risk_score(
    *,
    transitive_dependents: int,
    in_compromised_window: bool,
    maintainer_flagged: int = 0,
    maintainer_total: int = 0,
    typosquat_similarity: float | None = None,
    typosquat_count: int = 0,
) -> dict[str, Any]:
    """Blend the four signals into one 0.0-1.0 score plus its breakdown.

    The breakdown is returned alongside the score because a bare number is not
    actionable in an incident review — the UI shows which term dominated.
    """
    dependents = max(0, int(transitive_dependents))
    reach = math.log10(1 + dependents) / math.log10(1 + REACH_SATURATION)
    reach = min(1.0, reach)

    window = 1.0 if in_compromised_window else 0.0

    # Cluster exposure is a fraction, but a single flagged sibling is already
    # meaningful, so the floor is 0.5 once anything at all is flagged.
    if maintainer_flagged > 0 and maintainer_total > 0:
        maintainer = 0.5 + 0.5 * (maintainer_flagged / maintainer_total)
    elif maintainer_flagged > 0:
        maintainer = 0.5
    else:
        maintainer = 0.0
    maintainer = min(1.0, maintainer)

    # Similarity is already 0..1. Extra squatters add a small bounded premium —
    # capped so a swarm of weak impersonations cannot saturate the term on its
    # own and hide how close the nearest one actually is.
    proximity = float(typosquat_similarity or 0.0)
    if typosquat_count > 1:
        proximity = min(1.0, proximity + min(0.05, 0.01 * (typosquat_count - 1)))

    components = {
        "reach": reach,
        "window": window,
        "maintainer": maintainer,
        "typosquat": proximity,
    }
    score = sum(RISK_WEIGHTS[key] * value for key, value in components.items())
    score = round(min(1.0, max(0.0, score)), 4)

    return {
        "score": score,
        "band": _band(score),
        "weights": dict(RISK_WEIGHTS),
        "components": {key: round(value, 4) for key, value in components.items()},
        "contributions": {
            key: round(RISK_WEIGHTS[key] * value, 4) for key, value in components.items()
        },
        "signals": {
            "transitive_dependents": dependents,
            "in_compromised_window": bool(in_compromised_window),
            "maintainer_flagged": int(maintainer_flagged),
            "maintainer_packages": int(maintainer_total),
            "typosquat_similarity": round(proximity, 4) if proximity else 0.0,
            "typosquat_count": int(typosquat_count),
        },
    }


def package_risk_profile(
    *,
    closure: Mapping[str, Any],
    maintainer_risk_report: Mapping[str, Any] | None,
    typosquat_report: Mapping[str, Any] | None,
    in_compromised_window: bool,
) -> dict[str, Any]:
    """Convenience wrapper: pull the four signals out of the analysis results."""
    dependents = len(closure.get("affected_ids") or [])

    flagged = total = 0
    if maintainer_risk_report:
        sisters = maintainer_risk_report.get("sister_packages") or []
        total = len(sisters)
        flagged = sum(1 for sister in sisters if sister.get("flagged"))

    similarity = 0.0
    squat_count = 0
    if typosquat_report:
        candidates = typosquat_report.get("candidates") or []
        squat_count = len(candidates)
        # No `max` server-side and none needed here either — the candidate list
        # is already sorted closest-first by the typosquat query.
        for candidate in candidates:
            similarity = max(similarity, float(candidate.get("similarity_score") or 0.0))

    return composite_risk_score(
        transitive_dependents=dependents,
        in_compromised_window=in_compromised_window,
        maintainer_flagged=flagged,
        maintainer_total=total,
        typosquat_similarity=similarity,
        typosquat_count=squat_count,
    )


# --------------------------------------------------------------------------
# Maintainer risk network
# --------------------------------------------------------------------------


def primary_maintainer_id(client: HydraClient, package_id: int) -> tuple[int | None, float]:
    """The first maintainer of a package, plus the query's wire latency.

    Fixed-length and target-anchored, which the engine allows — only
    *variable*-length patterns are required to start from a fixed source.
    """
    result = client.execute(
        f"MATCH (p:{schema.PACKAGE} {{id: $pid}})-[:{schema.MAINTAINED_BY}]->(m:{schema.MAINTAINER}) "
        f"RETURN m.id AS id ORDER BY m.id LIMIT 1",
        {"pid": int(package_id)},
    )
    row = result.first
    maintainer_id = int(row["id"]) if row and row.get("id") is not None else None
    return maintainer_id, result.latency_ms


def maintainer_risk(
    client: HydraClient,
    maintainer_id: int,
    *,
    window_hours: int = 48,
    exclude_package_id: int | None = None,
) -> dict[str, Any] | None:
    """Two-hop maintainer subgraph with in-window publication flags.

    ``Package -[:MAINTAINED_BY]-> Maintainer -[:MAINTAINS]-> Other_Package``.
    The outward hop walks the materialised ``MAINTAINS`` inverse, which projects
    the sister packages' properties in the same statement — so no follow-up
    hydration scan of all 390 Package nodes is needed.

    Publication recency lives on ``Version`` nodes, joined to packages by the
    ``package_id`` property (the Version node's ``name`` is ``pkg@semver``, so a
    name join would silently match nothing). One statement fetches every
    in-window version in the graph; the join is local because there is no ``IN``.

    Returns ``None`` when the maintainer id does not exist, so the API layer can
    answer 404.
    """
    maintainer_id = int(maintainer_id)
    latency_ms = 0.0

    identity = client.execute(
        f"MATCH (m:{schema.MAINTAINER} {{id: $mid}}) RETURN m.id AS id, m.name AS name, "
        f"m.username AS username, m.email AS email, m.key_fingerprint AS key_fingerprint, "
        f"m.two_factor_enabled AS two_factor_enabled, m.credential_status AS credential_status, "
        f"m.is_compromised AS is_compromised, m.account_created_at AS account_created_at",
        {"mid": maintainer_id},
    )
    latency_ms += identity.latency_ms
    row = identity.first
    if row is None or row.get("id") is None:
        return None
    maintainer = graph_node(row["id"], {key: row.get(key) for key in identity.columns if key != "id"})

    owned = client.execute(
        f"MATCH (m:{schema.MAINTAINER} {{id: $mid}})-[:{schema.MAINTAINS}]->(p:{schema.PACKAGE}) "
        f"RETURN DISTINCT p.id AS id, p.name AS name, p.ecosystem AS ecosystem, "
        f"p.is_compromised AS is_compromised, p.risk_score AS risk_score, "
        f"p.downloads_weekly AS downloads_weekly, p.last_published_at AS last_published_at, "
        f"p.latest_version AS latest_version",
        {"mid": maintainer_id},
    )
    latency_ms += owned.latency_ms

    windowed = client.execute(
        f"MATCH (v:{schema.VERSION}) WHERE v.compromised_window = true "
        f"RETURN v.package_id AS package_id, v.semver AS semver, v.published_at AS published_at"
    )
    latency_ms += windowed.latency_ms

    in_window: dict[int, list[dict[str, Any]]] = {}
    for version_row in windowed.rows:
        package_id = version_row.get("package_id")
        if package_id is None:
            continue
        in_window.setdefault(int(package_id), []).append(
            {"semver": version_row.get("semver"), "published_at": version_row.get("published_at")}
        )

    packages = [
        graph_node(pkg_row["id"], {key: pkg_row.get(key) for key in owned.columns if key != "id"})
        for pkg_row in owned.rows
        if pkg_row.get("id") is not None
    ]

    # The epicentre is not a "sister" — it is the package that was breached.
    # simulate-breach names it explicitly; the standalone endpoint infers it
    # from `is_compromised` so both produce the same collateral-only count.
    epicentre: dict[str, Any] | None = None
    if exclude_package_id is not None:
        epicentre = next((p for p in packages if p["id"] == int(exclude_package_id)), None)
    if epicentre is None:
        compromised = [p for p in packages if p.get("is_compromised")]
        if len(compromised) == 1:
            epicentre = compromised[0]

    sisters: list[dict[str, Any]] = []
    flagged_count = 0
    for package in packages:
        if epicentre is not None and package["id"] == epicentre["id"]:
            continue
        releases = in_window.get(package["id"], [])
        node = dict(package)
        node["published_within_window"] = bool(releases)
        node["flagged"] = bool(releases or package.get("is_compromised"))
        if releases:
            earliest = sorted(releases, key=lambda r: str(r.get("published_at") or ""))[0]
            node["window_release"] = earliest.get("semver")
            node["published_at"] = earliest.get("published_at")
        if node["flagged"]:
            flagged_count += 1
        sisters.append(node)

    # Flagged first so the UI's list leads with the risk, then by name.
    sisters.sort(key=lambda pkg: (not pkg.get("flagged"), str(pkg.get("name") or "")))

    window_hours = int(window_hours)
    if not sisters:
        note = "this maintainer publishes no other packages"
    elif flagged_count:
        note = (
            f"{flagged_count} sister {_plural(flagged_count, 'package')} published "
            f"within {window_hours}h of the breach"
        )
    else:
        note = (
            f"{len(sisters)} sister {_plural(len(sisters), 'package')} share this maintainer; "
            f"none published within {window_hours}h of the breach"
        )
    if epicentre is not None:
        note += f" (epicentre: {epicentre.get('name')})"

    return {
        "maintainer": maintainer,
        "sister_packages": sisters,
        "flagged_count": flagged_count,
        "window_hours": window_hours,
        "epicentre": epicentre,
        "risk_note": note,
        "latency_ms": round(latency_ms, 3),
    }


# --------------------------------------------------------------------------
# Typosquats
# --------------------------------------------------------------------------


def typosquats(client: HydraClient, package_id: int) -> dict[str, Any] | None:
    """Packages joined to ``package_id`` by a ``TYPOSQUAT_OF`` edge.

    HydraDB has no string functions, so edit distance, similarity and attack
    type are computed at seed time and stored on the edge; this reads them back
    in the same statement that projects the candidate's own properties. Both
    orientations are queried because edge direction is the seeder's convention,
    not something this layer should depend on.

    Returns ``None`` when the package id does not exist (API answers 404).
    """
    package_id = int(package_id)
    latency_ms = 0.0

    target_result = client.execute(
        f"MATCH (p:{schema.PACKAGE} {{id: $pid}}) RETURN p.id AS id, p.name AS name, "
        f"p.ecosystem AS ecosystem, p.is_compromised AS is_compromised, "
        f"p.risk_score AS risk_score, p.downloads_weekly AS downloads_weekly",
        {"pid": package_id},
    )
    latency_ms += target_result.latency_ms
    row = target_result.first
    if row is None or row.get("id") is None:
        return None
    target = graph_node(row["id"], {k: row.get(k) for k in target_result.columns if k != "id"})

    candidate_projection = (
        "c.id AS id, c.name AS name, c.ecosystem AS ecosystem, "
        "c.risk_score AS risk_score, c.downloads_weekly AS downloads_weekly, "
        "c.is_compromised AS is_compromised, c.last_published_at AS last_published_at, "
        "c.typosquat_target AS typosquat_target, "
        "r.edit_distance AS edit_distance, r.similarity_score AS similarity_score, "
        "r.attack_type AS attack_type"
    )
    orientations = (
        f"MATCH (c:{schema.PACKAGE})-[r:{schema.TYPOSQUAT_OF}]->(t:{schema.PACKAGE} {{id: $pid}}) "
        f"RETURN {candidate_projection}",
        f"MATCH (t:{schema.PACKAGE} {{id: $pid}})-[r:{schema.TYPOSQUAT_OF}]->(c:{schema.PACKAGE}) "
        f"RETURN {candidate_projection}",
    )

    seen: dict[int, dict[str, Any]] = {}
    for query in orientations:
        result = client.execute(query, {"pid": package_id})
        latency_ms += result.latency_ms
        for candidate_row in result.rows:
            candidate_id = candidate_row.get("id")
            if candidate_id is None or int(candidate_id) == package_id:
                continue
            seen.setdefault(
                int(candidate_id),
                graph_node(
                    candidate_id,
                    {key: candidate_row.get(key) for key in result.columns if key != "id"},
                ),
            )

    candidates = list(seen.values())
    # Closest impersonation first: highest similarity, then smallest edit
    # distance. Ranking is local because the engine offers no min/max.
    candidates.sort(
        key=lambda c: (
            -float(c.get("similarity_score") or 0.0),
            int(c["edit_distance"]) if c.get("edit_distance") is not None else 99,
            str(c.get("name") or ""),
        )
    )
    return {"target": target, "candidates": candidates, "latency_ms": round(latency_ms, 3)}


# --------------------------------------------------------------------------
# Breach timeline
# --------------------------------------------------------------------------

#: Severity vocabulary the frontend colours against.
SEVERITY_ORDER = {"critical": 0, "high": 1, "warning": 2, "info": 3}


def _event(moment: Any, text: str, severity: str, kind: str) -> dict[str, Any]:
    return {"t": moment, "event": text, "severity": severity, "kind": kind}


def build_timeline(
    *,
    package: Mapping[str, Any],
    compromised_version: Mapping[str, Any] | None,
    safe_version: Mapping[str, Any] | None,
    versions: Sequence[Mapping[str, Any]] = (),
    maintainer_report: Mapping[str, Any] | None = None,
    typosquat_candidates: Sequence[Mapping[str, Any]] = (),
    blast: Mapping[str, Any] | None = None,
    fleet: Fleet | None = None,
    exposed_service_ids: Sequence[int] = (),
    affected_package_count: int = 0,
    window_hours: int = 48,
) -> list[dict[str, Any]]:
    """Ordered incident events for the UI's timeline rail.

    Every timestamp is graph-derived. The window open time is *inferred*: it is
    the earliest in-window publication across the maintainer's packages, because
    the credential theft itself leaves no node behind — only its consequences do.
    """
    package_name = str(package.get("name") or "package")
    events: list[dict[str, Any]] = []

    bad_semver = str((compromised_version or {}).get("semver") or "")
    bad_at = (compromised_version or {}).get("published_at")

    # 1. Last known-good release — the anchor the fix rolls back to.
    if safe_version and safe_version.get("published_at"):
        events.append(
            _event(
                safe_version["published_at"],
                f"{package_name}@{safe_version.get('semver')} published — last release "
                f"outside the compromise window",
                "info",
                "release",
            )
        )

    # 2. Window opens. Inferred from the earliest in-window publication.
    window_publishes: list[datetime] = []
    for version in versions:
        if version.get("compromised_window") and _parse_iso(version.get("published_at")):
            window_publishes.append(_parse_iso(version.get("published_at")))  # type: ignore[arg-type]
    maintainer_node = (maintainer_report or {}).get("maintainer") or {}
    for sister in (maintainer_report or {}).get("sister_packages") or []:
        if sister.get("published_within_window"):
            parsed = _parse_iso(sister.get("published_at"))
            if parsed is not None:
                window_publishes.append(parsed)

    if window_publishes:
        window_open = min(window_publishes)
        handle = maintainer_node.get("username") or maintainer_node.get("name") or "the maintainer"
        two_factor = maintainer_node.get("two_factor_enabled")
        detail = "2FA disabled" if two_factor is False else "credentials reused"
        events.append(
            _event(
                _iso(window_open - timedelta(minutes=1)),
                f"Compromise window opens — publish credentials for {handle} used from an "
                f"unrecognised host ({detail})",
                "warning",
                "credential",
            )
        )

    # 3. The malicious publish itself.
    if bad_at:
        handle = maintainer_node.get("username") or maintainer_node.get("name") or "unknown"
        events.append(
            _event(
                bad_at,
                f"Malicious {package_name}@{bad_semver} published to "
                f"{package.get('ecosystem') or 'npm'} by {handle}",
                "critical",
                "publish",
            )
        )

    # 4. Later releases of the victim that are still inside the window.
    for version in versions:
        if not version.get("compromised_window"):
            continue
        semver = str(version.get("semver") or "")
        if not semver or semver == bad_semver:
            continue
        events.append(
            _event(
                version.get("published_at"),
                f"{package_name}@{semver} published inside the window — also unsafe to pin",
                "high",
                "publish",
            )
        )

    # 5. Collateral: the same account's other packages.
    for sister in (maintainer_report or {}).get("sister_packages") or []:
        if not sister.get("published_within_window"):
            continue
        release = sister.get("window_release")
        label = f"{sister.get('name')}@{release}" if release else str(sister.get("name"))
        events.append(
            _event(
                sister.get("published_at"),
                f"Sister package {label} published by the same account inside the "
                f"{int(window_hours)}h window",
                "high",
                "sister",
            )
        )

    # 6. Impersonation packages riding the incident.
    for candidate in typosquat_candidates:
        published = candidate.get("last_published_at")
        if not published:
            continue
        distance = candidate.get("edit_distance")
        attack = candidate.get("attack_type")
        descriptor = f"edit distance {distance}" if distance is not None else "close name match"
        if attack:
            descriptor += f", {attack}"
        events.append(
            _event(
                published,
                f"Typosquat {candidate.get('name')} published ({descriptor})",
                "warning",
                "typosquat",
            )
        )

    events.sort(key=lambda e: (str(e.get("t") or ""), SEVERITY_ORDER.get(e["severity"], 9)))

    # 7. Detection closes the timeline — always last, always now.
    if blast:
        exposed = blast.get("exposed_services", 0)
        total = blast.get("total_services", 0)
        tier1 = blast.get("tier1_exposed", 0)
        summary = (
            f"Radix reverse closure: {affected_package_count} packages, "
            f"{blast.get('exposed_lockfiles', 0)}/{blast.get('total_lockfiles', 0)} lockfiles and "
            f"{exposed}/{total} services exposed ({blast.get('percentage', 0.0)}%), "
            f"{tier1} tier-1"
        )
        if fleet is not None and exposed_service_ids:
            names = fleet.names(exposed_service_ids)
            if names:
                summary += " — " + ", ".join(names)
        events.append(_event(now_iso(), summary, "critical", "detection"))

    return events
