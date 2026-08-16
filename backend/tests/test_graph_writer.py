"""Live-HydraDB verification for :mod:`app.ingest.graph_writer`.

Runs against the real engine, never against namespace ``radix``.

NAMESPACE NOTE (discovered by live probe, 2026-08-16): the deployed bearer
token's authorisation is *prefix-scoped*. The literal namespace ``radix-test``
is rejected outright — ``permission_denied: principal bearer principal is not
authorized to read graph scope radix-test/graphs/default`` — and fixing that
would mean editing the token file and restarting the live container. The
sub-namespace ``radix/test`` *is* authorised and fully isolated from ``radix``
(verified: writes there are invisible to the demo namespace), so it serves as
the test scratch namespace. Override with ``RADIX_TEST_NAMESPACE`` once the
deployment grants the literal ``radix-test`` scope.

Runs standalone (``backend/.venv/bin/python backend/tests/test_graph_writer.py``)
or under pytest; each test does its own setup so ordering never matters.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import schema
from app.hydra_client import HydraClient, Path as HydraPath
from app.ingest.graph_writer import GraphWriter, edge_id, levenshtein, normalize_name
from app.ingest.model import (
    Advisory,
    DepEdge,
    IngestReport,
    MaintainerInfo,
    PackageMeta,
    PackageRelease,
    ParsedLockfile,
    RepoScan,
)

TEST_NAMESPACE = os.environ.get("RADIX_TEST_NAMESPACE", "radix/test")

# A wrongly-configured environment must fail loudly before any write happens.
assert TEST_NAMESPACE not in ("radix", "radix-live"), (
    f"refusing to run destructive tests against protected namespace {TEST_NAMESPACE!r}"
)

MAL_ID = "MAL-2026-9999"
COMPROMISED = "left-pad-ng"
COMPROMISED_VERSION = "2.1.4"
REPO = "acme/checkout-web"
COMMIT = "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"


def _client() -> HydraClient:
    client = HydraClient(namespace=TEST_NAMESPACE)
    assert client.config.namespace == TEST_NAMESPACE
    return client


def _wipe(client: HydraClient) -> None:
    """DETACH DELETE everything in the scratch namespace, label by label.

    Batched deletes cannot use a labelled pattern, so ids are read first
    (same form as the seeder's --reset).
    """
    assert client.config.namespace == TEST_NAMESPACE  # belt and braces
    for label in schema.NODE_LABELS + ("Probe",):
        ids = [
            row["id"]
            for row in client.execute(f"MATCH (n:{label}) RETURN n.id AS id").rows
            if row.get("id") is not None
        ]
        for start in range(0, len(ids), 200):
            client.execute(
                "UNWIND $rows AS row MATCH (n {id: row.vertex}) DETACH DELETE n",
                {"rows": [{"vertex": i} for i in ids[start : start + 200]]},
            )


# --- The hand-built IR ------------------------------------------------------
# 10 packages (one scoped), 2 maintainers, 1 lockfile. The compromised package
# sits three hops from the service (service -> http-client -> url-parse-lite
# -> left-pad-ng) so the reverse closure is a real traversal, and
# "url-parse-1ite" (homoglyph 1 -> l) exercises the typosquat pass.


def _build_scan() -> RepoScan:
    releases = [
        PackageRelease("http-client", "4.2.0", direct=True),
        PackageRelease("@acme/ui-kit", "1.5.2", direct=True),
        PackageRelease("eslint-plugin-x", "3.0.1", dev=True, direct=True),
        PackageRelease("url-parse-lite", "2.3.1"),
        PackageRelease(COMPROMISED, COMPROMISED_VERSION,
                       integrity="sha512-deadbeefdeadbeefdeadbeef"),
        PackageRelease("querystring-x", "1.4.0"),
        PackageRelease("char-fill", "1.0.3"),
        PackageRelease("react-dom-lite", "18.2.0"),
        PackageRelease("react-lite", "18.2.0"),
        PackageRelease("url-parse-1ite", "0.0.2"),
    ]
    edges = [
        DepEdge(None, None, "http-client", "^4.0.0", "4.2.0"),
        DepEdge(None, None, "@acme/ui-kit", "^1.5.0", "1.5.2"),
        DepEdge(None, None, "eslint-plugin-x", "^3.0.0", "3.0.1", dev=True),
        DepEdge("http-client", "4.2.0", "url-parse-lite", "^2.0.0", "2.3.1"),
        DepEdge("url-parse-lite", "2.3.1", COMPROMISED, "~2.1.0", COMPROMISED_VERSION),
        DepEdge("url-parse-lite", "2.3.1", "querystring-x", "^1.1.0", "1.4.0"),
        DepEdge(COMPROMISED, COMPROMISED_VERSION, "char-fill", "^1.0.0", "1.0.3"),
        DepEdge("@acme/ui-kit", "1.5.2", "react-dom-lite", "^18.0.0", "18.2.0"),
        DepEdge("react-dom-lite", "18.2.0", "react-lite", "^18.2.0", "18.2.0"),
        DepEdge("react-lite", "18.2.0", "url-parse-1ite", "^0.0.1", "0.0.2"),
    ]
    lockfile = ParsedLockfile(
        path="package-lock.json",
        kind="npm",
        root_name="checkout-web",
        releases=releases,
        edges=edges,
    )
    return RepoScan(
        source=f"https://github.com/{REPO}",
        repo_name=REPO,
        repo_url=f"https://github.com/{REPO}",
        commit_hash=COMMIT,
        lockfiles=[lockfile],
    )


def _build_meta() -> dict[str, PackageMeta]:
    alice = MaintainerInfo("alice", "alice@example.dev")
    bob = MaintainerInfo("bob", "bob@example.dev")
    return {
        COMPROMISED: PackageMeta(
            COMPROMISED,
            maintainers=[alice],
            published_at={"2.1.3": "2026-06-11T08:00:00.000Z",
                          COMPROMISED_VERSION: "2026-08-01T10:00:00.000Z"},
            downloads_weekly=800_000,
            latest=COMPROMISED_VERSION,
        ),
        "char-fill": PackageMeta(
            "char-fill", maintainers=[alice],
            published_at={"1.0.3": "2025-02-01T00:00:00.000Z"},
            downloads_weekly=650_000, latest="1.0.3",
        ),
        "url-parse-lite": PackageMeta(
            "url-parse-lite", maintainers=[alice],
            published_at={"2.3.1": "2025-11-20T12:30:00.000Z"},
            downloads_weekly=3_200_000, latest="2.3.1",
        ),
        "http-client": PackageMeta(
            "http-client", maintainers=[bob],
            published_at={"4.2.0": "2025-09-14T09:00:00.000Z"},
            downloads_weekly=5_100_000, latest="4.2.0",
        ),
        "@acme/ui-kit": PackageMeta(
            "@acme/ui-kit", maintainers=[bob],
            published_at={"1.5.2": "2026-01-05T16:45:00.000Z"},
            downloads_weekly=90_000, latest="1.5.2",
        ),
        "react-lite": PackageMeta(
            "react-lite",
            published_at={"18.2.0": "2025-06-01T00:00:00.000Z"},
            downloads_weekly=12_000_000, latest="18.2.0",
        ),
        "url-parse-1ite": PackageMeta(
            "url-parse-1ite",
            published_at={"0.0.2": "2026-07-30T03:12:00.000Z"},
            downloads_weekly=40, latest="0.0.2",
        ),
    }


def _mal_advisory() -> Advisory:
    return Advisory(
        id=MAL_ID,
        package=COMPROMISED,
        summary="malicious postinstall exfiltrates env",
        malicious=True,
        published="2026-08-02T00:00:00Z",
        affected_versions=[COMPROMISED_VERSION],
        affected_ranges=[("2.1.4", "2.2.0")],
    )


def _count(client: HydraClient, label: str) -> int:
    return int(client.execute(f"MATCH (n:{label}) RETURN count(*) AS c").scalar("c", 0) or 0)


def _edge_count(client: HydraClient, edge_type: str) -> int:
    return int(
        client.execute(f"MATCH (a)-[r:{edge_type}]->(b) RETURN count(*) AS c").scalar("c", 0) or 0
    )


def _package_id(writer: GraphWriter, name: str) -> int:
    pkg_id = writer.known_packages().get(name)
    assert pkg_id is not None, f"package {name!r} missing from known_packages()"
    return pkg_id


def _ingested(client: HydraClient) -> tuple[GraphWriter, IngestReport]:
    """Fresh wipe + one full ingest — shared setup for the read-back tests."""
    _wipe(client)
    writer = GraphWriter(client)
    report = writer.ingest(_build_scan(), _build_meta(), [_mal_advisory()])
    return writer, report


# --- 1. the big-integer edge-id probe ---------------------------------------


def test_edge_id_is_62_bit_and_round_trips():
    """The contract's sha1 edge id survives MERGE + read-back bit-for-bit.

    ``r.id`` cannot be projected or filtered in a plain MATCH ("unbound
    variable r"), so the stored value is read back through ``algo.SSpaths``,
    whose relationship ``properties.id`` carries the seeder-assigned id.
    """
    client = _client()
    try:
        max62 = (1 << 62) - 1
        derived = edge_id(schema.DEPENDS_ON, 1_000_001, 1_000_002)
        assert derived < (1 << 62)

        client.execute(
            "UNWIND $rows AS row MERGE (n {id: row.vertex}) SET n:Probe, n.name = row.name",
            {"rows": [{"vertex": 990_001, "name": "probe-a"},
                      {"vertex": 990_002, "name": "probe-b"}]},
        )
        for eid in (max62, derived):
            client.execute(
                "UNWIND $rows AS row "
                "MATCH (s:Probe {id: row.src}), (d:Probe {id: row.dst}) "
                "MERGE (s)-[r:PROBE_EDGE {id: row.eid}]->(d) SET r.k = row.k",
                {"rows": [{"src": 990_001, "dst": 990_002, "eid": eid, "k": 1}]},
            )
        result = client.execute(
            "CALL algo.SSpaths({sourceNode: $src, relTypes: ['PROBE_EDGE'], "
            "maxLen: 1, pathCount: 10}) YIELD path RETURN path",
            {"src": 990_001},
        )
        stored = sorted(
            rel.properties["id"]
            for row in result.rows
            if isinstance(row.get("path"), HydraPath)
            for rel in row["path"].relationships
        )
        assert max62 in stored, f"2^62-1 truncated: stored={stored}"
        assert derived in stored, f"sha1-derived id truncated: stored={stored}"

        # MERGE on the same big id must not duplicate.
        client.execute(
            "UNWIND $rows AS row "
            "MATCH (s:Probe {id: row.src}), (d:Probe {id: row.dst}) "
            "MERGE (s)-[r:PROBE_EDGE {id: row.eid}]->(d) SET r.k = row.k",
            {"rows": [{"src": 990_001, "dst": 990_002, "eid": derived, "k": 2}]},
        )
        assert _edge_count(client, "PROBE_EDGE") == 2
    finally:
        client.execute(
            "UNWIND $rows AS row MATCH (n {id: row.vertex}) DETACH DELETE n",
            {"rows": [{"vertex": 990_001}, {"vertex": 990_002}]},
        )
        client.close()


# --- 2. ingest + read-back --------------------------------------------------


def test_ingest_writes_and_reads_back():
    client = _client()
    try:
        writer, report = _ingested(client)

        assert report.namespace == TEST_NAMESPACE
        assert report.services == 1 and report.lockfiles == 1
        assert report.packages == 10 and report.versions == 10
        assert report.maintainers == 2
        assert report.compromised_marked == 1
        assert report.statements > 0 and report.wire_seconds > 0

        # Counts per label, read back from the engine.
        assert _count(client, schema.SERVICE) == 1
        assert _count(client, schema.LOCKFILE) == 1
        assert _count(client, schema.PACKAGE) == 10
        assert _count(client, schema.VERSION) == 10
        assert _count(client, schema.MAINTAINER) == 2

        # 3 root + 7 package-level + 1 service->lockfile, each mirrored.
        assert _edge_count(client, schema.DEPENDS_ON) == 11
        assert _edge_count(client, schema.DEPENDED_ON_BY) == 11
        assert _edge_count(client, schema.MAINTAINED_BY) == 5
        assert _edge_count(client, schema.MAINTAINS) == 5
        # 10 package->version release edges + 10 lockfile->version pins.
        assert _edge_count(client, schema.RESOLVED_IN) == 20

        # One DEPENDS_ON read back with its constraint.
        rows = client.execute(
            f"MATCH (a:{schema.PACKAGE})-[r:{schema.DEPENDS_ON}]->(b:{schema.PACKAGE}) "
            "WHERE r.constraint = $c RETURN a.name AS src, b.name AS dst, "
            "r.constraint AS constraint, r.is_dev AS is_dev, "
            "r.transitive_depth AS depth",
            {"c": "~2.1.0"},
        ).rows
        assert rows == [{"src": "url-parse-lite", "dst": COMPROMISED,
                         "constraint": "~2.1.0", "is_dev": False, "depth": 3}]

        # The materialised inverse of that same edge exists.
        inverse = client.execute(
            f"MATCH (a:{schema.PACKAGE} {{id: $src}})-[r:{schema.DEPENDED_ON_BY}]->"
            f"(b:{schema.PACKAGE} {{id: $dst}}) RETURN count(*) AS c",
            {"src": _package_id(writer, COMPROMISED),
             "dst": _package_id(writer, "url-parse-lite")},
        ).scalar("c", 0)
        assert inverse == 1

        # Reverse closure from the compromised package reaches the service.
        closure = client.execute(
            f"MATCH (v {{id: $pkg}})-[:{schema.DEPENDED_ON_BY}*1..6]->(x) "
            "RETURN DISTINCT x.id AS id",
            {"pkg": _package_id(writer, COMPROMISED)},
        ).rows
        reached = {row["id"] for row in closure}
        service_id = client.execute(
            f"MATCH (s:{schema.SERVICE}) RETURN s.id AS id"
        ).scalar("id")
        assert service_id in reached, f"closure never reached the service: {sorted(reached)}"
        assert _package_id(writer, "url-parse-lite") in reached
        assert _package_id(writer, "http-client") in reached

        # Version carries package_id (the property Version->Package joins use).
        version_row = client.execute(
            f"MATCH (v:{schema.VERSION}) WHERE v.name = $n "
            "RETURN v.package_id AS package_id, v.published_at AS published_at, "
            "v.compromised_window AS window",
            {"n": f"{COMPROMISED}@{COMPROMISED_VERSION}"},
        ).first
        assert version_row is not None
        assert version_row["package_id"] == _package_id(writer, COMPROMISED)
        assert version_row["published_at"] == "2026-08-01T10:00:00.000Z"
        assert version_row["window"] is True  # set by the MAL advisory

        # Maintainer edge with derived `since` (earliest published_at).
        since = client.execute(
            f"MATCH (p:{schema.PACKAGE} {{id: $pid}})-[r:{schema.MAINTAINED_BY}]->"
            f"(m:{schema.MAINTAINER}) RETURN m.username AS username, r.since AS since",
            {"pid": _package_id(writer, COMPROMISED)},
        ).first
        assert since == {"username": "alice", "since": "2026-06-11T08:00:00.000Z"}

        # Typosquat pass: homoglyph "url-parse-1ite" -> "url-parse-lite".
        assert report.typosquats == 1
        squat = client.execute(
            f"MATCH (a:{schema.PACKAGE})-[r:{schema.TYPOSQUAT_OF}]->(b:{schema.PACKAGE}) "
            "RETURN a.name AS squatter, b.name AS target, "
            "r.edit_distance AS edit_distance, r.similarity_score AS similarity_score",
        ).rows
        assert squat == [{"squatter": "url-parse-1ite", "target": "url-parse-lite",
                          "edit_distance": 0, "similarity_score": 1.0}]

        # Scoped package landed intact.
        scoped = client.execute(
            f"MATCH (p:{schema.PACKAGE}) WHERE p.name = $n RETURN p.id AS id",
            {"n": "@acme/ui-kit"},
        ).scalar("id")
        assert scoped == _package_id(writer, "@acme/ui-kit")
    finally:
        client.close()


# --- 3. idempotent re-ingest ------------------------------------------------


def test_reingest_reuses_ids_and_counts_are_stable():
    client = _client()
    try:
        first_writer, first_report = _ingested(client)
        first_ids = first_writer.known_packages()

        before = {label: _count(client, label) for label in schema.NODE_LABELS}
        before_edges = {t: _edge_count(client, t) for t in schema.EDGE_TYPES}

        # A brand-new writer must rebuild the id registry from the graph alone.
        second_writer = GraphWriter(client)
        assert second_writer.known_packages() == first_ids

        second_report = second_writer.ingest(_build_scan(), _build_meta(), [_mal_advisory()])
        assert second_writer.known_packages() == first_ids

        after = {label: _count(client, label) for label in schema.NODE_LABELS}
        after_edges = {t: _edge_count(client, t) for t in schema.EDGE_TYPES}
        assert after == before, f"node counts changed on re-ingest: {before} -> {after}"
        assert after_edges == before_edges, (
            f"edge counts changed on re-ingest: {before_edges} -> {after_edges}"
        )

        # The report describes the same write set both times.
        for field_name in ("services", "lockfiles", "packages", "versions",
                           "maintainers", "depends_on", "resolved_in",
                           "maintained_by", "typosquats"):
            assert getattr(first_report, field_name) == getattr(second_report, field_name)
        # Already flagged, so the second run flags nothing new.
        assert second_report.compromised_marked == 0
    finally:
        client.close()


# --- 4. apply_advisories (the watcher path) ---------------------------------


def test_apply_advisories_marks_package_and_versions():
    client = _client()
    try:
        _wipe(client)
        writer = GraphWriter(client)
        writer.ingest(_build_scan(), _build_meta(), [])  # no advisories yet

        clean = client.execute(
            f"MATCH (p:{schema.PACKAGE}) WHERE p.name = $n "
            "RETURN p.is_compromised AS c",
            {"n": COMPROMISED},
        ).first
        assert clean == {"c": False}

        # The watcher builds its own writer from the live graph.
        watcher_writer = GraphWriter(client)
        assert watcher_writer.apply_advisories([_mal_advisory()]) == 1

        row = client.execute(
            f"MATCH (p:{schema.PACKAGE}) WHERE p.name = $n "
            "RETURN p.is_compromised AS compromised, p.risk_score AS risk, "
            "p.advisories AS advisories",
            {"n": COMPROMISED},
        ).first
        assert row == {"compromised": True, "risk": 1.0, "advisories": MAL_ID}

        window = client.execute(
            f"MATCH (v:{schema.VERSION}) WHERE v.name = $n "
            "RETURN v.compromised_window AS w",
            {"n": f"{COMPROMISED}@{COMPROMISED_VERSION}"},
        ).first
        assert window == {"w": True}

        # Re-applying the same advisory flags nothing new.
        assert watcher_writer.apply_advisories([_mal_advisory()]) == 0

        # A non-malicious advisory bumps risk without compromising.
        ghsa = Advisory(id="GHSA-xxxx-yyyy-zzzz", package="http-client",
                        summary="ReDoS", severity="moderate",
                        affected_versions=["4.2.0"])
        assert watcher_writer.apply_advisories([ghsa]) == 0
        bumped = client.execute(
            f"MATCH (p:{schema.PACKAGE}) WHERE p.name = $n "
            "RETURN p.is_compromised AS compromised, p.risk_score AS risk",
            {"n": "http-client"},
        ).first
        assert bumped is not None and bumped["compromised"] is False
        assert 0.05 < bumped["risk"] < 1.0
    finally:
        client.close()


# --- pure-python unit checks (no engine) ------------------------------------


def test_edge_id_is_deterministic_and_62_bit():
    a = edge_id("DEPENDS_ON", 1, 2)
    assert a == edge_id("DEPENDS_ON", 1, 2)
    assert a != edge_id("DEPENDED_ON_BY", 2, 1)
    assert 0 <= a < (1 << 62)


def test_typosquat_text_helpers():
    assert levenshtein("react", "reacct") == 1
    assert levenshtein("react", "preact", cap=2) == 1
    assert levenshtein("alpha", "omega", cap=2) == 3  # capped early exit
    assert normalize_name("url-parse-1ite") == normalize_name("url-parse-lite")
    assert normalize_name("event-strearn") == normalize_name("event-stream")  # rn -> m
    assert normalize_name("lodash") != normalize_name("underscore")


if __name__ == "__main__":
    tests = [
        test_edge_id_is_deterministic_and_62_bit,
        test_typosquat_text_helpers,
        test_edge_id_is_62_bit_and_round_trips,
        test_ingest_writes_and_reads_back,
        test_reingest_reuses_ids_and_counts_are_stable,
        test_apply_advisories_marks_package_and_versions,
    ]
    failures = 0
    print(f"namespace under test: {TEST_NAMESPACE!r}")
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — a runner reports, it does not raise
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
