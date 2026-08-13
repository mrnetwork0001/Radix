#!/usr/bin/env python3.12
"""One-command seeder for the Radix ecosystem graph.

    python3.12 scripts/seed_ecosystem.py --reset --verify

Loads the deterministic dataset from ``ecosystem_spec`` into a running HydraDB
over the HTTP query API. Only stdlib is used, so no virtualenv is required.

Everything here is shaped by the verified engine constraints in
``docs/HYDRADB_CONTRACT.md``:

* **Nodes** upsert as ``MERGE (n {id: row.vertex}) SET n:Label, n.x = row.x``.
  Extra properties cannot be folded into the ``MERGE`` pattern — the pattern is
  the identity being matched — so identity and payload are split.
* **Edges** upsert as ``MATCH (s:A {id: row.src}), (d:B {id: row.dst})
  MERGE (s)-[r:TYPE {id: row.eid}]->(d) SET ...``. Each endpoint needs exactly
  one label, and a relationship carrying properties needs an explicit
  ``id: row.<field>``.
* Because a batched statement has one fixed ``SET`` clause, rows are grouped by
  *property signature* before chunking. That also avoids ever writing a null.
* Deletes cannot use ``UNWIND`` with a labelled pattern
  ("UNWIND batch node patterns do not support labels"), so ``--reset`` reads the
  ids first and deletes them through an unlabelled batch.

Exit code is non-zero if ``--verify`` finds the graph does not match the spec.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ecosystem_spec import (  # noqa: E402
    BLAST_DEPTH,
    COMPROMISED_PACKAGE,
    COMPROMISED_VERSION,
    Ecosystem,
    Edge,
    Node,
    build_ecosystem,
)
from app.schema import (  # noqa: E402  (path bootstrapped by ecosystem_spec)
    DEPENDED_ON_BY,
    DEPENDS_ON,
    EDGE_TYPES,
    LOCKFILE,
    MAINTAINED_BY,
    MAINTAINER,
    MAINTAINS,
    NODE_LABELS,
    PACKAGE,
    RESOLVED_IN,
    SERVICE,
    TYPOSQUAT_OF,
    VERSION,
)

DEFAULT_URL = "http://127.0.0.1:8443/v1/graphs/default/query"
DEFAULT_TOKEN = "local-development-token-32-bytes"
DEFAULT_NAMESPACE = "radix"
DEFAULT_CELL = "cell-0"
DEFAULT_CHUNK = 200
#: Admission control rejects any request declaring more than this
#: ("client_query_runtime_ms rejected by admission control ... limit 30000").
MAX_QUERY_MS = 30_000

DATA_DIR = _HERE.parent / "data"


# --- HydraDB HTTP client ----------------------------------------------------


class HydraError(RuntimeError):
    pass


class HydraClient:
    def __init__(self, url: str, token: str, namespace: str, cell: str = DEFAULT_CELL,
                 timeout: float = 30.0, verbose: bool = False) -> None:
        self.url = url
        self.token = token
        self.namespace = namespace
        self.cell = cell
        self.timeout = timeout
        self.verbose = verbose
        self.statements = 0
        self.wire_seconds = 0.0

    def query(self, cypher: str, parameters: dict | None = None) -> dict:
        body = {
            "cell_id": self.cell,
            "query": " ".join(cypher.split()),  # one statement per request
            "parameters": parameters or {},
            "timeout_ms": min(int(self.timeout * 1000), MAX_QUERY_MS),
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Graph-Namespace": self.namespace,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise HydraError(f"HTTP {exc.code}: {detail}\n  query: {body['query'][:400]}") from None
        except urllib.error.URLError as exc:
            raise HydraError(f"cannot reach HydraDB at {self.url}: {exc.reason}") from None
        finally:
            self.wire_seconds += time.perf_counter() - started
            self.statements += 1
        if "error" in payload:
            raise HydraError(f"{payload['error']}\n  query: {body['query'][:400]}")
        if self.verbose:
            print(f"    -> {body['query'][:110]}")
        return payload

    # -- typed-value helpers ------------------------------------------------

    @staticmethod
    def _unwrap(cell: Any) -> Any:
        """HydraDB tags every scalar; unwrap to a plain Python value."""
        if not isinstance(cell, dict):
            return cell
        kind = cell.get("type")
        if kind == "null":
            return None
        if kind == "list":
            return [HydraClient._unwrap(v) for v in cell.get("value", [])]
        return cell.get("value")

    def rows(self, cypher: str, parameters: dict | None = None) -> list[dict]:
        payload = self.query(cypher, parameters)
        columns = payload.get("columns") or []
        return [dict(zip(columns, (self._unwrap(c) for c in row))) for row in payload.get("rows", [])]

    def scalar(self, cypher: str, parameters: dict | None = None, default: Any = None) -> Any:
        rows = self.rows(cypher, parameters)
        if not rows:
            return default
        return next(iter(rows[0].values()))


# --- Batching ---------------------------------------------------------------


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _set_clause(target: str, keys: Sequence[str]) -> str:
    return ", ".join(f"{target}.{key} = row.{key}" for key in keys)


def _group_nodes(nodes: Sequence[Node]) -> dict[tuple[str, tuple[str, ...]], list[Node]]:
    """Group by (label, property signature).

    One batched statement carries one fixed SET clause, so nodes that differ in
    which properties they define must go in different batches. Grouping this way
    also means no row ever has to supply a null placeholder.
    """
    groups: dict[tuple[str, tuple[str, ...]], list[Node]] = {}
    for node in nodes:
        groups.setdefault((node.label, tuple(sorted(node.props))), []).append(node)
    return groups


def _group_edges(edges: Sequence[Edge]) -> dict[tuple[str, str, str, tuple[str, ...]], list[Edge]]:
    groups: dict[tuple[str, str, str, tuple[str, ...]], list[Edge]] = {}
    for edge in edges:
        key = (edge.type, edge.src_label, edge.dst_label, tuple(sorted(edge.props)))
        groups.setdefault(key, []).append(edge)
    return groups


def write_nodes(client: HydraClient, nodes: Sequence[Node], chunk: int) -> int:
    written = 0
    for (label, keys), group in sorted(_group_nodes(nodes).items()):
        statement = (
            "UNWIND $rows AS row "
            "MERGE (n {id: row.vertex}) "
            f"SET n:{label}" + (", " + _set_clause("n", keys) if keys else "")
        )
        for batch in chunked(group, chunk):
            rows = [{"vertex": n.id, **n.props} for n in batch]
            client.query(statement, {"rows": rows})
            written += len(rows)
    return written


def write_edges(client: HydraClient, edges: Sequence[Edge], chunk: int) -> int:
    written = 0
    for (edge_type, src_label, dst_label, keys), group in sorted(_group_edges(edges).items()):
        statement = (
            "UNWIND $rows AS row "
            f"MATCH (s:{src_label} {{id: row.src}}), (d:{dst_label} {{id: row.dst}}) "
            # The explicit `id: row.eid` is mandatory for property-carrying
            # relationships, and it is what makes re-seeding idempotent.
            f"MERGE (s)-[r:{edge_type} {{id: row.eid}}]->(d)"
            + (" SET " + _set_clause("r", keys) if keys else "")
        )
        for batch in chunked(group, chunk):
            rows = [{"src": e.src, "dst": e.dst, "eid": e.id, **e.props} for e in batch]
            client.query(statement, {"rows": rows})
            written += len(rows)
    return written


# --- Reset ------------------------------------------------------------------


def reset(client: HydraClient, chunk: int) -> int:
    """Remove every Radix node (and, via DETACH, every edge) currently stored.

    Deletes go through an *unlabelled* UNWIND pattern because HydraDB rejects
    labels in a batched node pattern; the ids are read back per label first.
    """
    deleted = 0
    for label in NODE_LABELS:
        ids = [row["id"] for row in client.rows(f"MATCH (n:{label}) RETURN n.id AS id")]
        for batch in chunked(ids, chunk):
            client.query(
                "UNWIND $rows AS row MATCH (n {id: row.vertex}) DETACH DELETE n",
                {"rows": [{"vertex": i} for i in batch]},
            )
            deleted += len(batch)
        if ids:
            print(f"  deleted {len(ids):>4} {label}")
    return deleted


# --- Verification -----------------------------------------------------------


def _closure_ids(client: HydraClient, root_id: int, depth: int, label: str) -> list[int]:
    # Variable-length traversal must start from a fixed source id and run
    # forward, hence the materialised DEPENDED_ON_BY inverse edge.
    rows = client.rows(
        f"MATCH (v {{id: $root}})-[:{DEPENDED_ON_BY}*1..{depth}]->(x:{label}) "
        "RETURN DISTINCT x.id AS id",
        {"root": root_id},
    )
    return sorted({row["id"] for row in rows})


def verify(client: HydraClient, eco: Ecosystem) -> bool:
    meta = eco.meta
    incident = meta["incident"]
    victim_id = incident["package_id"]
    ok = True

    print("\n== node counts ==")
    for label in NODE_LABELS:
        actual = client.scalar(f"MATCH (n:{label}) RETURN count(*) AS c", default=0)
        expected = meta["node_counts"][label]
        flag = "ok " if actual == expected else "MISMATCH"
        ok &= actual == expected
        print(f"  {label:<11} {actual:>5}  (expected {expected:>5})  {flag}")
    total = sum(client.scalar(f"MATCH (n:{label}) RETURN count(*) AS c", default=0) for label in NODE_LABELS)
    print(f"  {'TOTAL':<11} {total:>5}  (expected {meta['total_nodes']:>5})")

    print("\n== edge counts ==")
    for edge_type in EDGE_TYPES:
        actual = client.scalar(f"MATCH ()-[r:{edge_type}]->() RETURN count(*) AS c", default=0)
        expected = meta["edge_counts"].get(edge_type, 0)
        flag = "ok " if actual == expected else "MISMATCH"
        ok &= actual == expected
        print(f"  {edge_type:<15} {actual:>5}  (expected {expected:>5})  {flag}")

    print(f"\n== reverse closure from {COMPROMISED_PACKAGE} (id {victim_id}) ==")
    services = {row["id"]: row for row in client.rows(
        "MATCH (s:Service) RETURN s.id AS id, s.name AS name, s.criticality AS criticality")}
    total_services = len(services)
    total_lockfiles = client.scalar("MATCH (n:Lockfile) RETURN count(*) AS c", default=0)

    for depth in (1, 2, 3, 4, 5, 6, 8):
        exposed = _closure_ids(client, victim_id, depth, SERVICE)
        pct = 100.0 * len(exposed) / total_services if total_services else 0.0
        marker = "  <-- headline" if depth == BLAST_DEPTH else ""
        print(f"  depth {depth}: {len(exposed):>2}/{total_services} services ({pct:5.1f}%){marker}")

    exposed = _closure_ids(client, victim_id, BLAST_DEPTH, SERVICE)
    packages = _closure_ids(client, victim_id, BLAST_DEPTH, PACKAGE)
    lockfiles = _closure_ids(client, victim_id, BLAST_DEPTH, LOCKFILE)
    tier1 = sum(1 for i in exposed if services[i]["criticality"] == "tier-1")

    print(f"\n  exposed services : {len(exposed)}/{total_services} "
          f"({100.0 * len(exposed) / total_services:.1f}%)")
    for i in exposed:
        print(f"      {services[i]['name']:<24} {services[i]['criticality']}")
    print(f"  tier-1 exposed   : {tier1}")
    print(f"  affected packages: {len(packages)}")
    print(f"  exposed lockfiles: {len(lockfiles)}/{total_lockfiles}")

    expected_blast = meta["blast_radius"]
    if len(exposed) != expected_blast["exposed_service_count"]:
        ok = False
        print(f"  MISMATCH: expected {expected_blast['exposed_service_count']} exposed services")
    if tier1 != expected_blast["tier1_exposed"]:
        ok = False
        print(f"  MISMATCH: expected tier1_exposed={expected_blast['tier1_exposed']}")

    print("\n== typosquats of tanstack-query (computed at seed time) ==")
    rows = client.rows(
        "MATCH (a:Package)-[r:TYPOSQUAT_OF]->(b:Package) WHERE b.id = $victim "
        "RETURN a.name AS name, r.edit_distance AS edit_distance, "
        "r.similarity_score AS similarity_score, r.attack_type AS attack_type "
        "ORDER BY r.similarity_score DESC",
        {"victim": victim_id},
    )
    for row in rows:
        print(f"  {row['name']:<20} distance={row['edit_distance']} "
              f"similarity={row['similarity_score']:.4f}  {row['attack_type']}")
    ok &= len(rows) == len(incident["typosquats"])

    print("\n== maintainer sentinel ==")
    rows = client.rows(
        "MATCH (m:Maintainer)-[:MAINTAINS]->(p:Package) WHERE m.id = $mid "
        "RETURN p.name AS name, p.last_published_at AS last_published_at, p.risk_score AS risk",
        {"mid": incident["maintainer_id"]},
    )
    window_start = incident["breach_at"]
    for row in rows:
        flagged = row["last_published_at"] >= window_start
        print(f"  {row['name']:<26} published {row['last_published_at']}"
              f"{'   <-- inside 48h window' if flagged else ''}")

    print("\n== compromised version ==")
    rows = client.rows(
        "MATCH (v:Version) WHERE v.package_id = $pid RETURN v.id AS id, v.semver AS semver, "
        "v.published_at AS published_at, v.compromised_window AS compromised_window "
        "ORDER BY v.published_at",
        {"pid": victim_id},
    )
    for row in rows:
        tag = " COMPROMISED" if row["compromised_window"] else ""
        print(f"  {row['semver']:<8} {row['published_at']}{tag}")
    ok &= any(r["semver"] == COMPROMISED_VERSION and r["compromised_window"] for r in rows)

    print("\nVERIFY: " + ("PASS" if ok else "FAIL"))
    return ok


# --- Snapshot ---------------------------------------------------------------


def write_snapshot(eco: Ecosystem) -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = DATA_DIR / "ecosystem_snapshot.json"
    manifest = DATA_DIR / "radix_manifest.json"
    snapshot.write_text(json.dumps(
        {
            "meta": eco.meta,
            "nodes": [{"id": n.id, "label": n.label, **n.props} for n in eco.nodes],
            "edges": [{"id": e.id, "type": e.type, "source": e.src, "target": e.dst, **e.props}
                      for e in eco.edges],
        },
        indent=1,
    ), encoding="utf-8")
    manifest.write_text(json.dumps(eco.meta, indent=2), encoding="utf-8")
    return snapshot, manifest


# --- CLI --------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Radix ecosystem graph into HydraDB.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--cell", default=DEFAULT_CELL)
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                        help="rows per UNWIND request (default 200)")
    parser.add_argument("--reset", action="store_true",
                        help="DETACH DELETE every existing Radix node first")
    parser.add_argument("--verify", action="store_true",
                        help="re-read the graph and check it against the spec")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and snapshot the dataset without writing to HydraDB")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="skip writing data/*.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    build_started = time.perf_counter()
    eco = build_ecosystem()
    build_ms = (time.perf_counter() - build_started) * 1000
    meta = eco.meta
    print(f"built ecosystem in {build_ms:.0f} ms  "
          f"({meta['total_nodes']} nodes, {meta['total_edges']} edges, seed {meta['seed']})")
    for label, count in meta["node_counts"].items():
        print(f"  {label:<11} {count:>5}")
    blast = meta["blast_radius"]
    print(f"  blast radius (local BFS, depth {BLAST_DEPTH}): "
          f"{blast['exposed_service_count']}/{blast['total_services']} services "
          f"({blast['exposed_pct']}%), tier-1 {blast['tier1_exposed']}")

    if not args.no_snapshot:
        snapshot, manifest = write_snapshot(eco)
        print(f"  snapshot -> {snapshot}")
        print(f"  manifest -> {manifest}")

    if args.dry_run:
        return 0

    client = HydraClient(args.url, args.token, args.namespace, args.cell, verbose=args.verbose)

    if args.reset:
        print("\nresetting...")
        removed = reset(client, args.chunk)
        print(f"  removed {removed} nodes")

    print("\nwriting nodes...")
    started = time.perf_counter()
    written_nodes = write_nodes(client, eco.nodes, args.chunk)
    print(f"  {written_nodes} nodes in {(time.perf_counter() - started):.2f}s")

    print("writing edges...")
    started = time.perf_counter()
    written_edges = write_edges(client, eco.edges, args.chunk)
    print(f"  {written_edges} edges in {(time.perf_counter() - started):.2f}s "
          f"(incl. {meta['edge_counts'].get(DEPENDED_ON_BY, 0)} materialised "
          f"{DEPENDED_ON_BY} + {meta['edge_counts'].get(MAINTAINS, 0)} {MAINTAINS} inverses)")
    print(f"  {client.statements} statements, {client.wire_seconds:.2f}s on the wire")

    if args.verify and not verify(client, eco):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
