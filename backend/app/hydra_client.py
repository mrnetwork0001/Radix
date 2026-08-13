"""HydraDB access layer for Radix.

Everything Radix knows about the supply-chain graph comes through this module.
It owns three responsibilities:

1. **Transport** — a pooled HTTP session against the HydraDB JSON query API,
   with per-call latency measured around the wire request only (the API contract
   requires ``latency_ms`` to exclude FastAPI serialisation).
2. **Decoding** — HydraDB returns *type-tagged* values (``{"type":"vertex_id",
   "value":4}``) and, inside ``path`` values, a second and *different* wrapper
   form for properties (``{"String":"x"}``). Both are unwrapped here so no
   caller upstream ever sees a tag.
3. **Traversal** — the four analysis queries plus the dashboard reads, shaped
   around the engine constraints in ``docs/HYDRADB_CONTRACT.md``.

Constraints that visibly bend the code (each verified by live probe, see the
module-level notes at each site):

* Variable-length ``MATCH`` runs forward from a fixed source id only, so reverse
  closure walks the materialised ``DEPENDED_ON_BY`` edge.
* The hop bound in ``*1..N`` must be a **literal** — ``*1..$depth`` is rejected
  with "unbounded variable-length MATCH requires an explicit max hop" — so the
  depth is validated as an int and interpolated, never parameterised.
* There is no ``IN`` operator, so every "these ids" filter is a label scan
  joined client-side.
* ``count(n)`` is rejected ("RETURN currently supports <binding>.<property> or
  count(*)"); only ``count(*)`` aggregates.
* A var-length ``MATCH`` projects bare vertex ids, never node objects, hence the
  separate hydration pass.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import requests

from . import schema

__all__ = [
    "HydraClient",
    "HydraQueryError",
    "HydraUnavailableError",
    "QueryResult",
    "Path",
    "PathNode",
    "PathRelationship",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class HydraError(RuntimeError):
    """Base class for every failure raised by this module."""


class HydraUnavailableError(HydraError):
    """The HydraDB node could not be reached at all (connection/timeout)."""


class HydraQueryError(HydraError):
    """HydraDB accepted the request but rejected or failed the query.

    Carries the parsed ``{"error": {"code", "message"}}`` envelope plus the
    originating query, because HydraDB's parse-time rejections name the exact
    unsupported construct and that message is the fastest route to a fix.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "unknown",
        query: str | None = None,
        status_code: int | None = None,
    ) -> None:
        detail = f"[{code}] {message}"
        if query:
            detail += f"\n  query: {query.strip()}"
        super().__init__(detail)
        self.code = code
        self.message = message
        self.query = query
        self.status_code = status_code


# --------------------------------------------------------------------------
# Value decoding
# --------------------------------------------------------------------------

#: Inner property wrappers seen inside ``path`` values. Verified by probe over a
#: node carrying every scalar type: ``{"String":"hello"}``, ``{"Integer":42}``,
#: ``{"SignedInteger":-7}``, ``{"Float":0.9375}``, ``{"Bool":true}``.
#: This encoding is *not* the same as the outer row tagging, which is why the
#: flattener below is separate from :func:`_unwrap`.
_PROPERTY_WRAPPERS = frozenset(
    {"String", "Integer", "SignedInteger", "Float", "Bool", "Boolean", "Null", "List"}
)


def _flatten_property(value: Any) -> Any:
    """Flatten one HydraDB property wrapper into a plain Python value.

    Wrappers are single-key dicts keyed by a Rust type name. Anything that is
    not a recognised wrapper is passed through unchanged, so an engine upgrade
    that starts emitting bare scalars degrades to a no-op rather than a crash.
    """
    if isinstance(value, dict) and len(value) == 1:
        (tag, inner), = value.items()
        if tag in _PROPERTY_WRAPPERS:
            if tag == "Null":
                return None
            if tag == "List":
                return [_flatten_property(item) for item in inner or []]
            return inner
    if isinstance(value, list):
        return [_flatten_property(item) for item in value]
    return value


def flatten_properties(properties: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten a whole HydraDB property map into plain Python values."""
    if not properties:
        return {}
    return {key: _flatten_property(val) for key, val in properties.items()}


@dataclass(frozen=True)
class PathNode:
    """A node as it appears inside a ``path`` value, properties flattened."""

    id: int
    labels: list[str]
    properties: dict[str, Any]

    @property
    def label(self) -> str | None:
        """The single label Radix nodes carry (HydraDB writes require one)."""
        return self.labels[0] if self.labels else None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "labels": list(self.labels), "properties": dict(self.properties)}


@dataclass(frozen=True)
class PathRelationship:
    """A relationship inside a ``path`` value.

    ``id`` is HydraDB's *internal* edge id, which is not the seeder-assigned
    ``id`` property — that one survives in :attr:`properties` under ``"id"``.
    Verified by probe: an edge written with ``id: 8899001`` came back as
    ``{"id": 15, ..., "properties": {"id": {"Integer": 8899001}}}``.
    """

    id: int
    edge_type: str
    src: int
    dst: int
    properties: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "edge_type": self.edge_type,
            "src": self.src,
            "dst": self.dst,
            "properties": dict(self.properties),
        }


@dataclass(frozen=True)
class Path:
    """A parsed ``path`` value: ``n`` nodes joined by ``n - 1`` relationships."""

    nodes: list[PathNode]
    relationships: list[PathRelationship]

    @property
    def node_ids(self) -> list[int]:
        """The vertex-id chain, source first — what the animation consumes."""
        return [node.id for node in self.nodes]

    @property
    def length(self) -> int:
        return len(self.relationships)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "relationships": [rel.to_dict() for rel in self.relationships],
        }


def _parse_path(value: Mapping[str, Any]) -> Path:
    nodes = [
        PathNode(
            id=int(node["id"]),
            labels=list(node.get("labels") or []),
            properties=flatten_properties(node.get("properties")),
        )
        for node in value.get("nodes") or []
    ]
    relationships = [
        PathRelationship(
            id=int(rel["id"]),
            edge_type=str(rel.get("edge_type") or ""),
            src=int(rel["src"]),
            dst=int(rel["dst"]),
            properties=flatten_properties(rel.get("properties")),
        )
        for rel in value.get("relationships") or []
    ]
    return Path(nodes=nodes, relationships=relationships)


def _unwrap(cell: Any) -> Any:
    """Unwrap one type-tagged response value into a plain Python value.

    Tags observed live: ``null`` (which arrives with *no* ``value`` key at all),
    ``vertex_id``, ``integer``, ``signed_integer``, ``float``, ``boolean``,
    ``string``, ``list`` (elements are themselves tagged), ``path``.
    """
    if not isinstance(cell, dict) or "type" not in cell:
        return cell  # already plain

    tag = cell.get("type")
    if tag == "null":
        return None

    value = cell.get("value")
    if tag == "list":
        return [_unwrap(item) for item in value or []]
    if tag == "path":
        return _parse_path(value or {})
    if tag in ("vertex_id", "integer", "signed_integer"):
        return None if value is None else int(value)
    if tag == "float":
        return None if value is None else float(value)
    if tag == "boolean":
        return None if value is None else bool(value)
    if tag == "string":
        return value
    # Unknown future tag: hand back the payload rather than failing the request.
    return value


@dataclass
class QueryResult:
    """One executed statement: unwrapped rows plus the measured wire latency."""

    columns: list[str]
    rows: list[dict[str, Any]]
    latency_ms: float
    query_id: str | None = None
    read_epoch: int | None = None
    bookmark: str | None = None

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    @property
    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def scalar(self, column: str | None = None, default: Any = None) -> Any:
        """First row's value for ``column`` (or the first column) — for counts."""
        if not self.rows:
            return default
        key = column if column is not None else (self.columns[0] if self.columns else None)
        if key is None:
            return default
        value = self.rows[0].get(key, default)
        return default if value is None else value

    def column(self, name: str) -> list[Any]:
        """Every non-null value in one column, in row order."""
        return [row[name] for row in self.rows if row.get(name) is not None]


# --------------------------------------------------------------------------
# Projection tables
# --------------------------------------------------------------------------

#: Properties projected per label. HydraDB cannot return a whole node
#: (``RETURN n`` is rejected: "RETURN currently supports <binding>.<property>
#: or count(*)"), so each label scan names its columns explicitly. Absent
#: properties come back as the ``null`` tag, which unwraps to ``None`` and is
#: dropped before the node dict reaches the API layer.
_LABEL_PROPERTIES: dict[str, tuple[str, ...]] = {
    schema.PACKAGE: ("name", "ecosystem", "is_compromised", "risk_score", "downloads_weekly"),
    schema.VERSION: ("name", "ecosystem", "semver", "published_at", "compromised_window"),
    schema.MAINTAINER: ("name", "username", "email", "key_fingerprint"),
    schema.SERVICE: ("name", "repo_url", "criticality"),
    schema.LOCKFILE: ("name", "filename", "commit_hash"),
}

#: Edge properties projected per public edge type, per the API contract.
#: ``DEPENDED_ON_BY`` / ``MAINTAINS`` are deliberately absent — they are
#: internal inverses and are never returned to the frontend.
_EDGE_PROPERTIES: dict[str, tuple[str, ...]] = {
    schema.DEPENDS_ON: ("constraint", "is_dev", "transitive_depth"),
    schema.MAINTAINED_BY: ("since",),
    schema.RESOLVED_IN: ("resolved_version",),
    schema.TYPOSQUAT_OF: ("edit_distance", "similarity_score"),
}

#: Longest closure HydraDB will be asked to walk. Guards the literal that gets
#: interpolated into ``*1..N``.
MAX_DEPTH = 8

#: ``algo.SSpaths`` defaults ``pathCount`` low enough that you get a single path
#: back, so Radix always sets it explicitly.
DEFAULT_PATH_COUNT = 400


def _node_dict(node_id: int, properties: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble an API-shaped node dict, dropping properties the label lacks."""
    label = schema.label_for_id(node_id)
    node: dict[str, Any] = {"id": node_id, "label": label}
    node.update({key: val for key, val in properties.items() if val is not None})
    node.setdefault("name", f"node-{node_id}")
    return node


def _clean_depth(depth: int) -> int:
    """Clamp and integer-ise a hop bound before it is interpolated into Cypher.

    The bound cannot be a parameter, so it is inlined — this function is the
    only reason that is safe.
    """
    try:
        value = int(depth)
    except (TypeError, ValueError):
        value = 6
    return max(1, min(MAX_DEPTH, value))


def _sspaths_query(rel_type: str, hops: int, path_count: int = DEFAULT_PATH_COUNT) -> str:
    """Build an ``algo.SSpaths`` call.

    ``relTypes`` and ``maxLen`` live inside a Cypher map literal and cannot be
    parameterised, so they are composed from validated inputs; only the source
    id travels as ``$src``.
    """
    return (
        "CALL algo.SSpaths({"
        f"sourceNode: $src, relTypes: ['{rel_type}'], "
        f"maxLen: {int(hops)}, pathCount: {int(path_count)}"
        "}) YIELD path RETURN path"
    )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass
class HydraConfig:
    """Connection settings, all overridable by environment variable."""

    http_url: str = field(default_factory=lambda: os.getenv("HYDRA_HTTP_URL", "http://127.0.0.1:8443"))
    admin_url: str = field(default_factory=lambda: os.getenv("HYDRA_ADMIN_URL", "http://127.0.0.1:9090"))
    token: str = field(default_factory=lambda: os.getenv("HYDRA_TOKEN", "local-development-token-32-bytes"))
    namespace: str = field(default_factory=lambda: os.getenv("HYDRA_NAMESPACE", "radix"))
    graph: str = field(default_factory=lambda: os.getenv("HYDRA_GRAPH", "default"))
    cell: str = field(default_factory=lambda: os.getenv("HYDRA_CELL", "cell-0"))
    timeout_ms: int = field(default_factory=lambda: int(os.getenv("HYDRA_TIMEOUT_MS", "30000")))
    page_size: int = field(default_factory=lambda: int(os.getenv("HYDRA_PAGE_SIZE", "4096")))

    @property
    def query_url(self) -> str:
        return f"{self.http_url.rstrip('/')}/v1/graphs/{self.graph}/query"

    @property
    def readyz_url(self) -> str:
        return f"{self.admin_url.rstrip('/')}/readyz"


class HydraClient:
    """Pooled client for one HydraDB graph namespace."""

    def __init__(
        self,
        http_url: str | None = None,
        token: str | None = None,
        namespace: str | None = None,
        graph: str | None = None,
        cell: str | None = None,
        *,
        admin_url: str | None = None,
        timeout_ms: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = HydraConfig()
        for attr, value in (
            ("http_url", http_url),
            ("admin_url", admin_url),
            ("token", token),
            ("namespace", namespace),
            ("graph", graph),
            ("cell", cell),
            ("timeout_ms", timeout_ms),
        ):
            if value is not None:
                setattr(self.config, attr, value)

        # One Session keeps the TCP connection warm across queries; the closure
        # endpoint issues several statements per request and reconnect cost
        # would otherwise dominate the latency number shown in the UI.
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.config.token}",
                "X-Graph-Namespace": self.config.namespace,
                "Content-Type": "application/json",
            }
        )
        self._owns_session = session is None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "HydraClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def execute(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> QueryResult:
        """Run one statement and return its unwrapped rows.

        ``latency_ms`` on the result brackets the HTTP round trip only.
        """
        budget_ms = timeout_ms or self.config.timeout_ms
        body: dict[str, Any] = {
            "cell_id": self.config.cell,
            "query": query,
            "parameters": dict(parameters or {}),
            "timeout_ms": budget_ms,
            "page_size": self.config.page_size,
        }

        started = time.perf_counter()
        try:
            response = self._session.post(
                self.config.query_url,
                json=body,
                # Requests wants seconds; add a small margin over the engine's
                # own budget so the server's timeout wins and reports a reason.
                timeout=(budget_ms / 1000.0) + 5.0,
            )
        except requests.RequestException as exc:
            raise HydraUnavailableError(f"HydraDB unreachable at {self.config.query_url}: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        try:
            payload = response.json()
        except ValueError as exc:
            raise HydraQueryError(
                f"non-JSON response (HTTP {response.status_code}): {response.text[:400]}",
                code="bad_response",
                query=query,
                status_code=response.status_code,
            ) from exc

        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"] or {}
            raise HydraQueryError(
                str(error.get("message", "unknown error")),
                code=str(error.get("code", "unknown")),
                query=query,
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise HydraQueryError(
                f"HTTP {response.status_code}: {response.text[:400]}",
                code="http_error",
                query=query,
                status_code=response.status_code,
            )

        columns: list[str] = list(payload.get("columns") or [])
        rows = [
            {column: _unwrap(cell) for column, cell in zip(columns, raw_row)}
            for raw_row in payload.get("rows") or []
        ]
        return QueryResult(
            columns=columns,
            rows=rows,
            latency_ms=elapsed_ms,
            query_id=payload.get("query_id"),
            read_epoch=payload.get("read_epoch"),
            bookmark=payload.get("bookmark"),
        )

    def ping(self, timeout_s: float = 2.0) -> tuple[bool, float]:
        """``(reachable, latency_ms)`` from a single trivial query.

        Uses a real query rather than ``/readyz`` because the dashboard's
        health badge is claiming the *query path* works, not just the process.
        """
        try:
            result = self.execute(
                f"MATCH (n:{schema.PACKAGE}) RETURN count(*) AS c",
                timeout_ms=int(timeout_s * 1000),
            )
        except HydraError:
            return False, 0.0
        return True, result.latency_ms

    # -- primitive reads ---------------------------------------------------

    def _scan_label(self, label: str, limit: int | None = None) -> tuple[list[dict[str, Any]], float]:
        """Every node of one label as an API-shaped dict.

        A label scan is Radix's substitute for ``WHERE id IN [...]``: HydraDB has
        no ``IN``, so the closure hydrates by scanning the handful of labels it
        touched and joining on id in Python.
        """
        properties = _LABEL_PROPERTIES.get(label, ("name",))
        projection = ", ".join(f"n.{prop} AS {prop}" for prop in properties)
        query = f"MATCH (n:{label}) RETURN n.id AS id, {projection}"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        result = self.execute(query)
        nodes = [
            _node_dict(row["id"], {key: row.get(key) for key in properties})
            for row in result.rows
            if row.get("id") is not None
        ]
        return nodes, result.latency_ms

    def _count_label(self, label: str) -> tuple[int, float]:
        # count(n) is rejected by the engine; count(*) is the only aggregate form.
        result = self.execute(f"MATCH (n:{label}) RETURN count(*) AS c")
        return int(result.scalar("c", 0) or 0), result.latency_ms

    def _read_edges(self, edge_type: str, limit: int | None = None) -> tuple[list[dict[str, Any]], float]:
        """All edges of one type as API-shaped dicts.

        Endpoints are deliberately unlabelled: the "exactly one label per
        endpoint" rule is a *write*-side constraint, and leaving reads
        unlabelled means one query covers every label pairing an edge type
        spans (``Service``→``Package``, ``Package``→``Package``, …).
        """
        properties = _EDGE_PROPERTIES.get(edge_type, ())
        projection = "".join(f", r.{prop} AS {prop}" for prop in properties)
        query = f"MATCH (a)-[r:{edge_type}]->(b) RETURN a.id AS source, b.id AS target{projection}"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        result = self.execute(query)
        edges: list[dict[str, Any]] = []
        for row in result.rows:
            if row.get("source") is None or row.get("target") is None:
                continue
            edge = {"source": row["source"], "target": row["target"], "type": edge_type}
            edge.update({key: row[key] for key in properties if row.get(key) is not None})
            edges.append(edge)
        return edges, result.latency_ms

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """One node by id, label inferred from the partitioned id space."""
        label = schema.label_for_id(int(node_id))
        if label is None:
            return None
        properties = _LABEL_PROPERTIES.get(label, ("name",))
        projection = ", ".join(f"n.{prop} AS {prop}" for prop in properties)
        result = self.execute(
            f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id, {projection}",
            {"id": int(node_id)},
        )
        row = result.first
        if row is None or row.get("id") is None:
            return None
        return _node_dict(row["id"], {key: row.get(key) for key in properties})

    def get_nodes(self, node_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Hydrate many ids at once, keyed by id.

        Without ``IN`` the choice is N point lookups or one scan per label
        touched; the scan wins as soon as more than a couple of ids share a
        label, which is the normal case for a closure.
        """
        wanted = {int(node_id) for node_id in node_ids}
        if not wanted:
            return {}
        by_label: dict[str, set[int]] = {}
        for node_id in wanted:
            label = schema.label_for_id(node_id)
            if label is not None:
                by_label.setdefault(label, set()).add(node_id)

        hydrated: dict[int, dict[str, Any]] = {}
        for label, ids in by_label.items():
            nodes, _ = self._scan_label(label)
            for node in nodes:
                if node["id"] in ids:
                    hydrated[node["id"]] = node
        # Ids with no stored node still get a placeholder so downstream joins
        # never KeyError on a graph the seeder is still filling in.
        for node_id in wanted - set(hydrated):
            hydrated[node_id] = _node_dict(node_id, {})
        return hydrated

    def resolve_package(self, name_or_id: int | str) -> dict[str, Any] | None:
        """Look up a package by integer id or by exact name.

        The API accepts ``package_name`` wherever it accepts ``package_id``, and
        HydraDB has no ``CONTAINS``, so name resolution is exact equality first
        and a ``STARTS WITH`` prefix match as a fallback.
        """
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.lstrip("-").isdigit()):
            node_id = int(name_or_id)
            node = self.get_node(node_id)
            if node is not None:
                return node
            # A numeric string that is not a live vertex id may still be a name.
            if isinstance(name_or_id, int):
                return None

        name = str(name_or_id)
        properties = _LABEL_PROPERTIES[schema.PACKAGE]
        projection = ", ".join(f"n.{prop} AS {prop}" for prop in properties)
        for predicate in ("n.name = $name", "n.name STARTS WITH $name"):
            result = self.execute(
                f"MATCH (n:{schema.PACKAGE}) WHERE {predicate} "
                f"RETURN n.id AS id, {projection} LIMIT 1",
                {"name": name},
            )
            row = result.first
            if row is not None and row.get("id") is not None:
                return _node_dict(row["id"], {key: row.get(key) for key in properties})
        return None

    # -- 1. transitive closure --------------------------------------------

    def get_transitive_closure(self, package_id: int, depth: int = 6) -> dict[str, Any]:
        """Reverse dependency closure: everything that transitively depends on ``package_id``.

        This is Radix's headline query and the one the engine makes awkward.
        ``MATCH (x)-[:DEPENDS_ON*1..6]->(victim {id: $pkg})`` is rejected —
        variable-length traversal must start from a fixed source — so the walk
        runs *forward* along the seeder's materialised ``DEPENDED_ON_BY``
        inverse, which arrives at the same set.

        Returns affected ids partitioned by label, hydrated node dicts, and the
        ``algo.SSpaths`` id-chains that drive the infection animation.
        """
        package_id = int(package_id)
        hops = _clean_depth(depth)

        # The hop bound must be a literal — `*1..$d` is rejected outright.
        closure = self.execute(
            f"MATCH (victim {{id: $pkg}})-[:{schema.DEPENDED_ON_BY}*1..{hops}]->(dependent) "
            f"RETURN DISTINCT dependent.id AS id",
            {"pkg": package_id},
        )
        affected_ids = sorted({int(row["id"]) for row in closure.rows if row.get("id") is not None})
        affected_ids = [node_id for node_id in affected_ids if node_id != package_id]

        paths_result = self._shortest_paths(package_id, hops)
        latency_ms = closure.latency_ms + paths_result[1]

        by_label: dict[str, list[int]] = {label: [] for label in schema.NODE_LABELS}
        for node_id in affected_ids:
            label = schema.label_for_id(node_id)
            if label in by_label:
                by_label[label].append(node_id)

        hydrated = self.get_nodes(affected_ids)
        root = self.get_node(package_id) or _node_dict(package_id, {})

        return {
            "root": root,
            "depth": hops,
            "latency_ms": round(latency_ms, 3),
            "affected_ids": affected_ids,
            "affected_package_ids": by_label[schema.PACKAGE],
            "affected_version_ids": by_label[schema.VERSION],
            "affected_maintainer_ids": by_label[schema.MAINTAINER],
            "affected_service_ids": by_label[schema.SERVICE],
            "affected_lockfile_ids": by_label[schema.LOCKFILE],
            "affected_nodes": [hydrated[node_id] for node_id in affected_ids],
            "paths": paths_result[0],
        }

    def _shortest_paths(self, source_id: int, hops: int) -> tuple[list[list[int]], float]:
        """Infection routes out of ``source_id`` as vertex-id chains, root first.

        A plain ``MATCH`` projects endpoints, not routes, so whole paths come
        from the native ``algo.SSpaths`` procedure. ``pathCount`` defaults low
        enough to return a single path, so it is always set explicitly.
        """
        try:
            result = self.execute(
                _sspaths_query(schema.DEPENDED_ON_BY, hops),
                {"src": int(source_id)},
            )
        except HydraQueryError:
            # Path extraction is presentational; a closure without an animation
            # is still a correct answer, so this never fails the request.
            return [], 0.0

        chains: list[list[int]] = []
        seen: set[tuple[int, ...]] = set()
        for row in result.rows:
            path = row.get("path")
            if not isinstance(path, Path):
                continue
            chain = path.node_ids
            key = tuple(chain)
            if len(chain) > 1 and key not in seen:
                seen.add(key)
                chains.append(chain)
        chains.sort(key=lambda chain: (len(chain), chain))
        return chains, result.latency_ms

    def get_closure_paths(self, package_id: int, depth: int = 6) -> tuple[list[Path], float]:
        """Full parsed :class:`Path` objects (nodes + relationships), not just ids."""
        try:
            result = self.execute(
                _sspaths_query(schema.DEPENDED_ON_BY, _clean_depth(depth)),
                {"src": int(package_id)},
            )
        except HydraQueryError:
            return [], 0.0
        paths = [row["path"] for row in result.rows if isinstance(row.get("path"), Path)]
        return paths, result.latency_ms

    # -- 2. maintainer risk network ---------------------------------------

    def get_maintainer_risk_network(self, maintainer_id: int, window_hours: int = 48) -> dict[str, Any]:
        """Two-hop walk ``Package -> MAINTAINED_BY -> Maintainer -> MAINTAINS -> Other_Package``.

        A compromised maintainer account is a shared blast surface: every other
        package they publish is suspect, and the ones they *also* published
        inside the breach window are the ones to flag.
        """
        maintainer_id = int(maintainer_id)
        latency_ms = 0.0

        maintainer = self.get_node(maintainer_id) or _node_dict(maintainer_id, {})

        # Hop 2, forward along the materialised inverse: the maintainer's other
        # packages. Fixed-length traversal, so the inverse edge is a
        # convenience here rather than a necessity.
        outward = self.execute(
            f"MATCH (m:{schema.MAINTAINER} {{id: $mid}})-[:{schema.MAINTAINS}]->(p:{schema.PACKAGE}) "
            f"RETURN DISTINCT p.id AS id",
            {"mid": maintainer_id},
        )
        latency_ms += outward.latency_ms
        sister_ids = {int(row["id"]) for row in outward.rows if row.get("id") is not None}

        # Hop 1, target-anchored: packages that name this maintainer. Legal
        # because the pattern is fixed-length — only *variable*-length patterns
        # must be source-anchored. Also carries the MAINTAINED_BY `since` prop.
        inward = self.execute(
            f"MATCH (p:{schema.PACKAGE})-[r:{schema.MAINTAINED_BY}]->(m:{schema.MAINTAINER} {{id: $mid}}) "
            f"RETURN p.id AS id, r.since AS since",
            {"mid": maintainer_id},
        )
        latency_ms += inward.latency_ms
        since_by_package: dict[int, Any] = {}
        for row in inward.rows:
            if row.get("id") is None:
                continue
            sister_ids.add(int(row["id"]))
            if row.get("since") is not None:
                since_by_package[int(row["id"])] = row["since"]

        ordered_ids = sorted(sister_ids)
        hydrated = self.get_nodes(ordered_ids)

        # Publication recency lives on Version nodes. One Version label scan,
        # indexed client-side by (ecosystem, name) — cheaper and more robust
        # than a per-package edge walk, and it works whichever way the seeder
        # orients the Package/Version relationship.
        versions, version_latency = self._scan_label(schema.VERSION)
        latency_ms += version_latency
        versions_by_package: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
        for version in versions:
            key = (version.get("ecosystem"), version.get("name"))
            versions_by_package.setdefault(key, []).append(version)

        sisters: list[dict[str, Any]] = []
        flagged_count = 0
        for node_id in ordered_ids:
            node = dict(hydrated[node_id])
            key = (node.get("ecosystem"), node.get("name"))
            candidates = versions_by_package.get(key, [])
            in_window = any(bool(v.get("compromised_window")) for v in candidates)
            published_at = None
            for version in candidates:
                if version.get("compromised_window") and version.get("published_at"):
                    published_at = version["published_at"]
                    break
            node["published_within_window"] = in_window
            node["flagged"] = bool(in_window or node.get("is_compromised"))
            if published_at is not None:
                node["published_at"] = published_at
            if node.get("flagged"):
                flagged_count += 1
            if node_id in since_by_package:
                node["maintained_since"] = since_by_package[node_id]
            sisters.append(node)

        # Flagged packages sort first so the UI's list leads with the risk.
        sisters.sort(key=lambda pkg: (not pkg.get("flagged"), pkg.get("name") or ""))

        if not sisters:
            note = "no other packages are published by this maintainer"
        elif flagged_count:
            note = (
                f"{flagged_count} sister package{'s' if flagged_count != 1 else ''} "
                f"published within {int(window_hours)}h of the breach"
            )
        else:
            note = (
                f"{len(sisters)} sister package{'s' if len(sisters) != 1 else ''} share this "
                f"maintainer; none published within {int(window_hours)}h of the breach"
            )

        return {
            "maintainer": maintainer,
            "sister_packages": sisters,
            "flagged_count": flagged_count,
            "window_hours": int(window_hours),
            "risk_note": note,
            "latency_ms": round(latency_ms, 3),
        }

    def maintainers_for_package(self, package_id: int) -> list[dict[str, Any]]:
        """The maintainers of one package, hydrated."""
        result = self.execute(
            f"MATCH (p:{schema.PACKAGE} {{id: $pid}})-[:{schema.MAINTAINED_BY}]->(m:{schema.MAINTAINER}) "
            f"RETURN DISTINCT m.id AS id",
            {"pid": int(package_id)},
        )
        ids = [int(row["id"]) for row in result.rows if row.get("id") is not None]
        hydrated = self.get_nodes(ids)
        return [hydrated[node_id] for node_id in ids]

    # -- 3. typosquat detection -------------------------------------------

    def detect_typosquats(self, package_id: int) -> dict[str, Any]:
        """Packages linked to ``package_id`` by a ``TYPOSQUAT_OF`` edge.

        HydraDB has no string functions, so edit distance and similarity are
        computed at seed time and stored on the edge; this reads them back.
        Both orientations are queried and merged, because whether the edge runs
        squatter→legitimate or the reverse is the seeder's convention, not a
        guarantee this layer should depend on.
        """
        package_id = int(package_id)
        target = self.get_node(package_id) or _node_dict(package_id, {})
        latency_ms = 0.0
        scores: dict[int, dict[str, Any]] = {}

        directions = (
            # candidate is the source: (squatter)-[:TYPOSQUAT_OF]->(target)
            f"MATCH (c:{schema.PACKAGE})-[r:{schema.TYPOSQUAT_OF}]->(t:{schema.PACKAGE} {{id: $pid}}) "
            f"RETURN c.id AS id, r.edit_distance AS edit_distance, r.similarity_score AS similarity_score",
            # candidate is the target: (target)-[:TYPOSQUAT_OF]->(squatter)
            f"MATCH (t:{schema.PACKAGE} {{id: $pid}})-[r:{schema.TYPOSQUAT_OF}]->(c:{schema.PACKAGE}) "
            f"RETURN c.id AS id, r.edit_distance AS edit_distance, r.similarity_score AS similarity_score",
        )
        for query in directions:
            result = self.execute(query, {"pid": package_id})
            latency_ms += result.latency_ms
            for row in result.rows:
                if row.get("id") is None:
                    continue
                node_id = int(row["id"])
                if node_id == package_id:
                    continue
                scores.setdefault(
                    node_id,
                    {
                        "edit_distance": row.get("edit_distance"),
                        "similarity_score": row.get("similarity_score"),
                    },
                )

        hydrated = self.get_nodes(scores)
        candidates: list[dict[str, Any]] = []
        for node_id, edge_props in scores.items():
            node = dict(hydrated[node_id])
            node.update({key: val for key, val in edge_props.items() if val is not None})
            candidates.append(node)

        # Closest first. `min`/`max` are unavailable server-side anyway, so all
        # ranking is Python's job — HydraDB traverses, Python scores.
        candidates.sort(
            key=lambda c: (
                -(c.get("similarity_score") or 0.0),
                c.get("edit_distance") if c.get("edit_distance") is not None else 99,
                c.get("name") or "",
            )
        )
        return {
            "target": target,
            "candidates": candidates,
            "latency_ms": round(latency_ms, 3),
        }

    # -- 4. blast radius ---------------------------------------------------

    def calculate_blast_radius(
        self,
        exposed_service_ids: Sequence[int],
        exposed_lockfile_ids: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        """Exposure of the fleet: services and lockfiles hit, versus the totals.

        Tier-1 status is a property of the Service nodes, and with no ``IN``
        operator there is no server-side way to fetch "criticality for these
        ids" — so one Service label scan is joined against the exposed set in
        Python.
        """
        exposed_services = {int(sid) for sid in exposed_service_ids or ()}
        exposed_lockfiles = {int(lid) for lid in exposed_lockfile_ids or ()}

        total_services, l1 = self._count_label(schema.SERVICE)
        total_lockfiles, l2 = self._count_label(schema.LOCKFILE)
        latency_ms = l1 + l2

        tier1_exposed = 0
        if exposed_services:
            services, l3 = self._scan_label(schema.SERVICE)
            latency_ms += l3
            for service in services:
                criticality = str(service.get("criticality") or "").lower()
                if service["id"] in exposed_services and criticality in ("tier-1", "tier1", "tier_1", "critical"):
                    tier1_exposed += 1

        percentage = (len(exposed_services) / total_services * 100.0) if total_services else 0.0
        return {
            "exposed_services": len(exposed_services),
            "total_services": total_services,
            "percentage": round(percentage, 1),
            "exposed_lockfiles": len(exposed_lockfiles),
            "total_lockfiles": total_lockfiles,
            "tier1_exposed": tier1_exposed,
            "latency_ms": round(latency_ms, 3),
        }

    # -- dashboard reads ---------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Node/edge tallies for the dashboard header."""
        latency_ms = 0.0
        counts: dict[str, int] = {}
        for label in schema.NODE_LABELS:
            count, elapsed = self._count_label(label)
            counts[label] = count
            latency_ms += elapsed

        total_edges = 0
        for edge_type in _EDGE_PROPERTIES:
            result = self.execute(f"MATCH (a)-[r:{edge_type}]->(b) RETURN count(*) AS c")
            latency_ms += result.latency_ms
            total_edges += int(result.scalar("c", 0) or 0)

        compromised = self.execute(
            f"MATCH (n:{schema.PACKAGE}) WHERE n.is_compromised = true RETURN count(*) AS c"
        )
        latency_ms += compromised.latency_ms

        return {
            "total_nodes": sum(counts.values()),
            "total_edges": total_edges,
            "packages": counts[schema.PACKAGE],
            "versions": counts[schema.VERSION],
            "maintainers": counts[schema.MAINTAINER],
            "services": counts[schema.SERVICE],
            "lockfiles": counts[schema.LOCKFILE],
            "compromised_packages": int(compromised.scalar("c", 0) or 0),
            "tracked_lockfiles": counts[schema.LOCKFILE],
            "latency_ms": round(latency_ms, 3),
        }

    def get_full_graph(self, limit: int | None = None) -> dict[str, Any]:
        """The whole graph for the visualiser: nodes, public edges, stats.

        ``limit`` caps *nodes*; edges are then filtered to those whose endpoints
        both survived, so the frontend never receives a dangling line.
        ``DEPENDED_ON_BY`` and ``MAINTAINS`` are excluded by construction — they
        are internal inverses and would draw every dependency twice.
        """
        latency_ms = 0.0
        nodes: list[dict[str, Any]] = []
        for label in schema.NODE_LABELS:
            label_nodes, elapsed = self._scan_label(label)
            latency_ms += elapsed
            nodes.extend(label_nodes)
        nodes.sort(key=lambda n: n["id"])

        if limit is not None and limit >= 0:
            nodes = nodes[: int(limit)]
        node_ids = {node["id"] for node in nodes}

        edges: list[dict[str, Any]] = []
        for edge_type in _EDGE_PROPERTIES:
            type_edges, elapsed = self._read_edges(edge_type)
            latency_ms += elapsed
            edges.extend(
                edge
                for edge in type_edges
                if edge["source"] in node_ids and edge["target"] in node_ids
            )

        stats = self.get_stats()
        latency_ms += stats.pop("latency_ms", 0.0)
        return {
            "nodes": nodes,
            "edges": edges,
            "stats": stats,
            "latency_ms": round(latency_ms, 3),
        }

    # -- helpers used by the API layer ------------------------------------

    def get_versions_for_package(self, package_id: int) -> list[dict[str, Any]]:
        """Version nodes for a package, newest-looking first.

        Matched on ``(ecosystem, name)`` from a Version label scan rather than
        by edge, so it holds whichever direction the Package/Version
        relationship is seeded in.
        """
        package = self.get_node(int(package_id))
        if package is None:
            return []
        versions, _ = self._scan_label(schema.VERSION)
        matches = [
            version
            for version in versions
            if version.get("name") == package.get("name")
            and version.get("ecosystem") == package.get("ecosystem")
        ]
        matches.sort(key=lambda v: (v.get("published_at") or "", v.get("semver") or ""), reverse=True)
        return matches

    def get_lockfiles_for_services(self, service_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
        """Lockfiles owned by each service, keyed by service id.

        Two edge reads plus a client-side join. Both orientations of the
        Service/Lockfile ``DEPENDS_ON`` edge are queried because that direction
        is the seeder's convention, and a per-id fan-out is not an option — no
        ``IN`` means "these service ids" cannot be expressed server-side.
        """
        wanted = {int(sid) for sid in service_ids or ()}
        if not wanted:
            return {}

        pairs: set[tuple[int, int]] = set()
        orientations = (
            (
                f"MATCH (s:{schema.SERVICE})-[:{schema.DEPENDS_ON}]->(l:{schema.LOCKFILE}) "
                f"RETURN s.id AS service_id, l.id AS lockfile_id"
            ),
            (
                f"MATCH (l:{schema.LOCKFILE})-[:{schema.DEPENDS_ON}]->(s:{schema.SERVICE}) "
                f"RETURN s.id AS service_id, l.id AS lockfile_id"
            ),
        )
        for query in orientations:
            result = self.execute(query)
            for row in result.rows:
                if row.get("service_id") is None or row.get("lockfile_id") is None:
                    continue
                service_id = int(row["service_id"])
                if service_id in wanted:
                    pairs.add((service_id, int(row["lockfile_id"])))

        hydrated = self.get_nodes(lockfile_id for _, lockfile_id in pairs)
        grouped: dict[int, list[dict[str, Any]]] = {sid: [] for sid in wanted}
        for service_id, lockfile_id in sorted(pairs):
            grouped[service_id].append(hydrated[lockfile_id])
        return grouped
