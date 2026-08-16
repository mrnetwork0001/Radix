"""Graph writer: the only ingestion module that talks to HydraDB.

Consumes the IR in :mod:`app.ingest.model` and writes it into one HydraDB
namespace using the verified forms from ``docs/HYDRADB_CONTRACT.md`` §4:

* nodes upsert as ``MERGE (n {id: row.vertex}) SET n:Label, n.x = row.x``,
  batched with ``UNWIND $rows`` and grouped by *property signature* (one
  batched statement has one fixed SET clause, and grouping also means no row
  ever writes a null);
* edges upsert as ``MATCH (s:A {id: row.src}), (d:B {id: row.dst})
  MERGE (s)-[r:TYPE {id: row.eid}]->(d) SET ...`` - exactly one label per
  endpoint, explicit ``id: row.eid`` on every relationship;
* every ``DEPENDS_ON`` is mirrored as ``DEPENDED_ON_BY`` and every
  ``MAINTAINED_BY`` as ``MAINTAINS``, because variable-length traversal only
  runs forward from a fixed source id.

**Node identity - the graph is the id registry.** At init the target
namespace is label-scanned for existing ``natural key -> id`` per label, and
new ids are allocated sequentially after each label's highest existing offset
(the partitions in ``schema._BASES``). Re-ingesting the same repo therefore
MERGEs onto the same ids.

**Edge identity - deterministic 62-bit hashes.** Verified by live probe on
2026-08-16 (see ``backend/tests/test_graph_writer.py``): a relationship ``id``
property of ``2**62 - 1`` (4611686018427387903) round-trips *exactly* through
``MERGE`` + read-back - HydraDB stores it as ``Integer`` (u64), so the full
62-bit hash is safe and no 53-bit fallback is needed. Two engine wrinkles the
same probes surfaced:

* ``r.id`` can never be referenced in a plain ``MATCH`` - projection *or*
  ``WHERE`` - it fails with "unbound variable r". The stored edge id is only
  readable through a path value's ``properties.id`` (``algo.SSpaths``).
* ``HydraClient`` does not follow ``next_cursor``, so registry scans paginate
  with ``ORDER BY n.id SKIP/LIMIT`` (both verified supported).
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .. import schema
from ..hydra_client import HydraClient
from .model import Advisory, IngestReport, PackageMeta, RepoScan

__all__ = ["GraphWriter", "edge_id", "levenshtein", "normalize_name"]


# --- Tunables ---------------------------------------------------------------

#: Rows per UNWIND request, matching the seeder's verified batch size.
CHUNK_ROWS = 200

#: SKIP/LIMIT page size for registry label scans (must stay under the
#: HydraClient page_size so a page is never silently truncated).
SCAN_PAGE = 2000

RISK_DEFAULT = 0.05          # a package we know nothing bad about
RISK_BUMP = 0.15             # per newly-seen non-malicious advisory
RISK_CAP = 0.9               # non-malicious advisories saturate below 1.0
RISK_MALICIOUS = 1.0         # 1.0 *means* malicious, per the contract

TYPOSQUAT_MAX_DISTANCE = 2
#: Only names this popular are worth squatting; keeps the O(candidates x
#: targets) pass proportionate and mirrors the contract's "high-download".
TYPOSQUAT_MIN_DOWNLOADS = 1_000_000


# --- Deterministic edge ids -------------------------------------------------


def edge_id(edge_type: str, src_id: int, dst_id: int) -> int:
    """62-bit deterministic relationship id, per the ingestion contract.

    ``sha1`` keyed by (type, src, dst) so re-ingestion MERGEs the same edge
    instead of duplicating it. 62 bits was probed live: HydraDB round-trips
    the full value exactly (Integer/u64 encoding).
    """
    digest = hashlib.sha1(f"{edge_type}|{src_id}|{dst_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 2


# --- Typosquat text machinery ----------------------------------------------

#: Single-character look-alikes, folded before measuring distance so that
#: "url-parse-1ite" collapses onto "url-parse-lite". Separators are stripped
#: because "node-fetch"/"nodefetch" is a classic squat shape.
_HOMOGLYPH_CHARS = str.maketrans(
    {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g",
     "-": "", "_": "", ".": ""}
)


def normalize_name(name: str) -> str:
    """Homoglyph-normalised, separator-free, lowercase form of a package name."""
    folded = name.lower().translate(_HOMOGLYPH_CHARS)
    # Multi-character look-alikes.
    return folded.replace("rn", "m").replace("vv", "w")


def levenshtein(a: str, b: str, cap: int | None = None) -> int:
    """Classic edit distance with an optional early-exit ``cap``."""
    if a == b:
        return 0
    if cap is not None and abs(len(a) - len(b)) > cap:
        return cap + 1
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, start=1):
        current = [i]
        best = i
        for j, ch_b in enumerate(b, start=1):
            cost = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ch_a != ch_b),
            )
            current.append(cost)
            best = min(best, cost)
        if cap is not None and best > cap:
            return cap + 1
        previous = current
    return previous[-1]


def _scope_of(name: str) -> str | None:
    if name.startswith("@") and "/" in name:
        return name.split("/", 1)[0]
    return None


# --- npm range checks (defensive: semver_npm may not exist yet) -------------

_satisfies_fn: Callable[[str, str], bool] | None = None


def _load_satisfies() -> Callable[[str, str], bool] | None:
    """Lazily import ``app.semver_npm.satisfies``.

    Another module owns that file and may land after this one; a missing or
    broken import degrades to exact-version matching rather than failing the
    watcher loop. A successful import is cached; a failure is retried on the
    next call so the function is picked up as soon as the file exists.
    """
    global _satisfies_fn
    if _satisfies_fn is None:
        try:
            from .. import semver_npm  # noqa: PLC0415 - deliberate lazy import

            _satisfies_fn = semver_npm.satisfies
        except Exception:
            return None
    return _satisfies_fn


def _version_affected(semver: str, advisory: Advisory) -> bool:
    """Does ``advisory`` cover this exact stored version?"""
    if semver in advisory.affected_versions:
        return True
    if not advisory.affected_ranges:
        return False
    satisfies = _load_satisfies()
    for introduced, fixed in advisory.affected_ranges:
        if satisfies is not None:
            range_ = f">={introduced or '0.0.0'}"
            if fixed:
                range_ += f" <{fixed}"
            try:
                if satisfies(semver, range_):
                    return True
            except Exception:
                continue  # a range the checker cannot parse never flags
        elif introduced == semver:
            # No range engine available: only the boundary version is provably
            # inside the window.
            return True
    return False


# --- Row containers ---------------------------------------------------------


@dataclass
class _NodeRow:
    label: str
    vertex: int
    props: dict[str, Any]


@dataclass
class _EdgeRow:
    type: str
    src: int
    src_label: str
    dst: int
    dst_label: str
    props: dict[str, Any]

    @property
    def eid(self) -> int:
        return edge_id(self.type, self.src, self.dst)


# --- Id registry -------------------------------------------------------------


#: Properties each label scan projects to rebuild that label's natural key.
_KEY_PROPS: dict[str, tuple[str, ...]] = {
    schema.PACKAGE: ("name", "ecosystem"),
    schema.VERSION: ("name", "ecosystem"),
    schema.MAINTAINER: ("name", "username"),
    schema.SERVICE: ("name",),
    schema.LOCKFILE: ("name",),
}


def _natural_key(label: str, row: Mapping[str, Any]) -> str | None:
    """Rebuild the ``schema`` natural key from a scanned node's properties."""
    name = row.get("name")
    if not name:
        return None
    if label == schema.PACKAGE:
        return schema.package_key(row.get("ecosystem") or "npm", name)
    if label == schema.VERSION:
        # A Version is named "<package>@<semver>", which is exactly the
        # name-part of version_key.
        return f"{row.get('ecosystem') or 'npm'}:{name}"
    if label == schema.MAINTAINER:
        return schema.maintainer_key(row.get("username") or name)
    if label == schema.SERVICE:
        return schema.service_key(name)
    if label == schema.LOCKFILE:
        return f"lockfile:{name}"
    return None


@dataclass
class _Registry:
    """Natural key -> vertex id, seeded from the live namespace at init."""

    by_key: dict[tuple[str, str], int] = field(default_factory=dict)
    next_offset: dict[str, int] = field(default_factory=dict)

    def id_for(self, label: str, key: str) -> tuple[int, bool]:
        """``(vertex_id, created)`` - allocation after the label's high mark."""
        cache_key = (label, key)
        existing = self.by_key.get(cache_key)
        if existing is not None:
            return existing, False
        offset = self.next_offset.get(label, 0)
        if offset >= schema._SPAN:
            raise OverflowError(f"id partition exhausted for label {label}")
        self.next_offset[label] = offset + 1
        vertex = schema._BASES[label] + offset
        self.by_key[cache_key] = vertex
        return vertex, True


# --- The writer --------------------------------------------------------------


class GraphWriter:
    """Writes ingestion IR into the HydraDB namespace of ``client``."""

    def __init__(self, client: HydraClient) -> None:
        self.client = client
        self.statements = 0
        self.wire_seconds = 0.0
        self._registry = _Registry()
        self._package_ids: dict[str, int] = {}  # package name -> vertex id
        self._load_registry()

    # -- transport bookkeeping --------------------------------------------

    def _run(self, query: str, parameters: Mapping[str, Any] | None = None):
        result = self.client.execute(query, parameters)
        self.statements += 1
        self.wire_seconds += result.latency_ms / 1000.0
        return result

    def _scan_label(self, label: str, props: tuple[str, ...]) -> list[dict[str, Any]]:
        """Every node of ``label``, paginated with SKIP/LIMIT.

        ``HydraClient`` does not follow ``next_cursor``, so a single query
        would silently truncate at its page size on a large namespace.
        """
        projection = ", ".join(f"n.{prop} AS {prop}" for prop in props)
        rows: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = self._run(
                f"MATCH (n:{label}) RETURN n.id AS id, {projection} "
                f"ORDER BY n.id SKIP {skip} LIMIT {SCAN_PAGE}"
            ).rows
            rows.extend(row for row in page if row.get("id") is not None)
            if len(page) < SCAN_PAGE:
                return rows
            skip += SCAN_PAGE

    # -- id registry -------------------------------------------------------

    def _load_registry(self) -> None:
        for label in schema.NODE_LABELS:
            base = schema._BASES[label]
            high = -1
            for row in self._scan_label(label, _KEY_PROPS[label]):
                vertex = int(row["id"])
                offset = vertex - base
                if not 0 <= offset < schema._SPAN:
                    continue  # foreign id - never allocate around it
                high = max(high, offset)
                key = _natural_key(label, row)
                if key is not None:
                    self._registry.by_key[(label, key)] = vertex
                if label == schema.PACKAGE and row.get("name"):
                    self._package_ids[row["name"]] = vertex
            self._registry.next_offset[label] = high + 1

    def known_packages(self) -> dict[str, int]:
        """Package ``name -> vertex id`` for the watcher's polling loop."""
        return dict(self._package_ids)

    # -- batched writes ----------------------------------------------------

    def _write_nodes(self, nodes: Iterable[_NodeRow]) -> int:
        """MERGE-by-id + SET, grouped by (label, property signature)."""
        groups: dict[tuple[str, tuple[str, ...]], list[_NodeRow]] = {}
        deduped: dict[int, _NodeRow] = {}
        for node in nodes:
            merged = deduped.get(node.vertex)
            if merged is None:
                deduped[node.vertex] = _NodeRow(node.label, node.vertex, dict(node.props))
            else:
                # A None never overwrites a value another row supplied.
                merged.props.update({k: v for k, v in node.props.items() if v is not None})
        for node in deduped.values():
            props = {k: v for k, v in node.props.items() if v is not None}
            groups.setdefault((node.label, tuple(sorted(props))), []).append(
                _NodeRow(node.label, node.vertex, props)
            )
        written = 0
        for (label, keys), group in sorted(groups.items()):
            set_clause = ", ".join(f"n.{key} = row.{key}" for key in keys)
            statement = (
                "UNWIND $rows AS row MERGE (n {id: row.vertex}) "
                f"SET n:{label}" + (", " + set_clause if set_clause else "")
            )
            for start in range(0, len(group), CHUNK_ROWS):
                batch = group[start : start + CHUNK_ROWS]
                self._run(statement, {"rows": [{"vertex": n.vertex, **n.props} for n in batch]})
                written += len(batch)
        return written

    def _write_edges(self, edges: Iterable[_EdgeRow]) -> int:
        """Idempotent edge upsert, grouped by (type, labels, signature)."""
        deduped: dict[int, _EdgeRow] = {}
        for edge in edges:
            deduped.setdefault(edge.eid, edge)
        groups: dict[tuple[str, str, str, tuple[str, ...]], list[_EdgeRow]] = {}
        for edge in deduped.values():
            props = {k: v for k, v in edge.props.items() if v is not None}
            groups.setdefault(
                (edge.type, edge.src_label, edge.dst_label, tuple(sorted(props))), []
            ).append(_EdgeRow(edge.type, edge.src, edge.src_label, edge.dst, edge.dst_label, props))
        written = 0
        for (edge_type, src_label, dst_label, keys), group in sorted(groups.items()):
            set_clause = ", ".join(f"r.{key} = row.{key}" for key in keys)
            statement = (
                "UNWIND $rows AS row "
                f"MATCH (s:{src_label} {{id: row.src}}), (d:{dst_label} {{id: row.dst}}) "
                f"MERGE (s)-[r:{edge_type} {{id: row.eid}}]->(d)"
                + (" SET " + set_clause if set_clause else "")
            )
            for start in range(0, len(group), CHUNK_ROWS):
                batch = group[start : start + CHUNK_ROWS]
                self._run(
                    statement,
                    {"rows": [{"src": e.src, "dst": e.dst, "eid": e.eid, **e.props} for e in batch]},
                )
                written += len(batch)
        return written

    # -- ingest ------------------------------------------------------------

    def ingest(
        self,
        scan: RepoScan,
        meta: dict[str, PackageMeta],
        advisories: list[Advisory],
    ) -> IngestReport:
        statements_before = self.statements
        wire_before = self.wire_seconds

        nodes: list[_NodeRow] = []
        edges: list[_EdgeRow] = []
        counts = {label: set() for label in schema.NODE_LABELS}
        edge_counts: dict[str, set[int]] = {t: set() for t in schema.EDGE_TYPES}

        def add_node(label: str, vertex: int, props: dict[str, Any]) -> None:
            counts[label].add(vertex)
            nodes.append(_NodeRow(label, vertex, props))

        def add_edge(edge_type: str, src: int, src_label: str, dst: int, dst_label: str,
                     props: dict[str, Any]) -> None:
            row = _EdgeRow(edge_type, src, src_label, dst, dst_label, props)
            edge_counts[edge_type].add(row.eid)
            edges.append(row)

        default_eco = "npm"
        for lockfile in scan.lockfiles:
            for release in lockfile.releases:
                default_eco = release.ecosystem
                break

        # Service --------------------------------------------------------
        service_id, _ = self._registry.id_for(schema.SERVICE, schema.service_key(scan.repo_name))
        add_node(schema.SERVICE, service_id, {
            "name": scan.repo_name,
            "repo_url": scan.repo_url,
        })

        # Packages: every name the lockfiles mention -----------------------
        package_names: dict[str, str] = {}  # name -> ecosystem
        for lockfile in scan.lockfiles:
            for release in lockfile.releases:
                package_names.setdefault(release.name, release.ecosystem)
            for dep in lockfile.edges:
                package_names.setdefault(dep.dst_name, default_eco)
                if dep.src_name is not None:
                    package_names.setdefault(dep.src_name, default_eco)

        package_id_of: dict[str, int] = {}
        for name, eco in package_names.items():
            pkg_id, created = self._registry.id_for(schema.PACKAGE, schema.package_key(eco, name))
            package_id_of[name] = pkg_id
            self._package_ids[name] = pkg_id
            props: dict[str, Any] = {"name": name, "ecosystem": eco}
            info = meta.get(name)
            if info is not None:
                props["downloads_weekly"] = info.downloads_weekly
                props["latest_version"] = info.latest
            if created:
                # Baseline state only on first sight: re-ingest must never
                # reset flags the advisory watcher has since raised.
                props.update({
                    "is_compromised": False,
                    "is_typosquat": False,
                    "risk_score": RISK_DEFAULT,
                })
            add_node(schema.PACKAGE, pkg_id, props)

        # Versions ---------------------------------------------------------
        version_pins: set[tuple[str, str]] = set()
        integrity_of: dict[tuple[str, str], str | None] = {}
        for lockfile in scan.lockfiles:
            for release in lockfile.releases:
                version_pins.add((release.name, release.version))
                integrity_of.setdefault((release.name, release.version), release.integrity)
            for dep in lockfile.edges:
                version_pins.add((dep.dst_name, dep.dst_version))
                if dep.src_name is not None and dep.src_version is not None:
                    version_pins.add((dep.src_name, dep.src_version))

        version_id_of: dict[tuple[str, str], int] = {}
        for name, semver in sorted(version_pins):
            eco = package_names.get(name, default_eco)
            vid, created = self._registry.id_for(
                schema.VERSION, schema.version_key(eco, name, semver)
            )
            version_id_of[(name, semver)] = vid
            pkg_id = package_id_of[name]
            info = meta.get(name)
            props = {
                "name": f"{name}@{semver}",
                "semver": semver,
                "ecosystem": eco,
                # Version->Package joins run on this property (a Version's
                # `name` is "<package>@<semver>", so a name join matches
                # nothing - see git 631c570).
                "package_id": pkg_id,
                "package_name": name,
                "published_at": info.published_at.get(semver) if info else None,
                "integrity": integrity_of.get((name, semver)),
            }
            if created:
                props["compromised_window"] = False
            add_node(schema.VERSION, vid, props)
            # Package release history stays walkable, per the demo world.
            add_edge(schema.RESOLVED_IN, pkg_id, schema.PACKAGE, vid, schema.VERSION,
                     {"resolved_version": semver})

        # Lockfiles --------------------------------------------------------
        for lockfile in scan.lockfiles:
            lock_id, _ = self._registry.id_for(
                schema.LOCKFILE, schema.lockfile_key(scan.repo_name, lockfile.path)
            )
            add_node(schema.LOCKFILE, lock_id, {
                "name": f"{scan.repo_name}/{lockfile.path}",
                "filename": lockfile.path,
                "kind": lockfile.kind,
                "ecosystem": default_eco,
                "commit_hash": scan.commit_hash,
                "service_id": service_id,
                "service_name": scan.repo_name,
                "entry_count": len(lockfile.releases),
            })
            # Keeps the lockfile attached to its service in the visualiser
            # and in get_lockfiles_for_services().
            add_edge(schema.DEPENDS_ON, service_id, schema.SERVICE, lock_id, schema.LOCKFILE,
                     {"constraint": "lockfile", "is_dev": False, "transitive_depth": 0})
            add_edge(schema.DEPENDED_ON_BY, lock_id, schema.LOCKFILE, service_id, schema.SERVICE,
                     {"transitive_depth": 0})
            for release in lockfile.releases:
                vid = version_id_of[(release.name, release.version)]
                add_edge(schema.RESOLVED_IN, lock_id, schema.LOCKFILE, vid, schema.VERSION,
                         {"resolved_version": release.version})

        # Dependency edges -------------------------------------------------
        # transitive_depth = shortest hop count from the root manifest,
        # estimated by BFS over the name-level dependency graph.
        adjacency: dict[str | None, set[str]] = {}
        for lockfile in scan.lockfiles:
            for dep in lockfile.edges:
                adjacency.setdefault(dep.src_name, set()).add(dep.dst_name)
        depth_of: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque(
            (dst, 1) for dst in sorted(adjacency.get(None, ()))
        )
        while queue:
            name, depth = queue.popleft()
            if name in depth_of:
                continue
            depth_of[name] = depth
            for nxt in sorted(adjacency.get(name, ())):
                if nxt not in depth_of:
                    queue.append((nxt, depth + 1))

        for lockfile in scan.lockfiles:
            for dep in lockfile.edges:
                dst_id = package_id_of[dep.dst_name]
                if dep.src_name is None:
                    src_id, src_label = service_id, schema.SERVICE
                else:
                    src_id, src_label = package_id_of[dep.src_name], schema.PACKAGE
                if src_id == dst_id:
                    continue
                props: dict[str, Any] = {
                    "constraint": dep.constraint,
                    "is_dev": dep.dev,
                    "transitive_depth": depth_of.get(dep.dst_name),  # None -> omitted
                }
                add_edge(schema.DEPENDS_ON, src_id, src_label, dst_id, schema.PACKAGE, props)
                add_edge(schema.DEPENDED_ON_BY, dst_id, schema.PACKAGE, src_id, src_label,
                         {"transitive_depth": depth_of.get(dep.dst_name)})

        # Maintainers ------------------------------------------------------
        maintained_pairs: set[tuple[int, int]] = set()
        for name, pkg_id in package_id_of.items():
            info = meta.get(name)
            if info is None:
                continue
            # `since` = earliest published_at of this maintainer's versions of
            # the package, when the registry gave us any timestamps at all
            # (ISO-8601 strings order lexicographically).
            since = min(info.published_at.values()) if info.published_at else None
            for maintainer in info.maintainers:
                mid, _ = self._registry.id_for(
                    schema.MAINTAINER, schema.maintainer_key(maintainer.username)
                )
                add_node(schema.MAINTAINER, mid, {
                    "name": maintainer.username,
                    "username": maintainer.username,
                    "email": maintainer.email,
                })
                if (pkg_id, mid) in maintained_pairs:
                    continue
                maintained_pairs.add((pkg_id, mid))
                add_edge(schema.MAINTAINED_BY, pkg_id, schema.PACKAGE, mid, schema.MAINTAINER,
                         {"since": since})
                add_edge(schema.MAINTAINS, mid, schema.MAINTAINER, pkg_id, schema.PACKAGE,
                         {"since": since})

        # Write, then the derived passes ----------------------------------
        self._write_nodes(nodes)
        self._write_edges(edges)
        compromised_marked = self.apply_advisories(advisories)
        typosquats = self._write_typosquats(sorted(package_names))

        return IngestReport(
            namespace=self.client.config.namespace,
            services=len(counts[schema.SERVICE]),
            lockfiles=len(counts[schema.LOCKFILE]),
            packages=len(counts[schema.PACKAGE]),
            versions=len(counts[schema.VERSION]),
            maintainers=len(counts[schema.MAINTAINER]),
            depends_on=len(edge_counts[schema.DEPENDS_ON]),
            resolved_in=len(edge_counts[schema.RESOLVED_IN]),
            maintained_by=len(edge_counts[schema.MAINTAINED_BY]),
            typosquats=typosquats,
            compromised_marked=compromised_marked,
            statements=self.statements - statements_before,
            wire_seconds=round(self.wire_seconds - wire_before, 3),
        )

    # -- advisories (also the watcher's entry point) -----------------------

    def apply_advisories(self, advisories: list[Advisory]) -> int:
        """Mark compromised packages/versions; returns packages *newly* flagged.

        ``MAL-`` (or ``malicious``) advisories set ``is_compromised`` and pin
        ``risk_score`` to 1.0. Ordinary advisories bump the score, capped
        below 1.0. Advisory ids accumulate on the Package as a comma-joined
        string (property values are scalars only). Versions inside an
        advisory's ranges/exact list get ``compromised_window = true``.
        """
        if not advisories:
            return 0

        # Fresh state, not the init snapshot: the watcher holds one writer
        # across many polls and ingest() may have run in between.
        packages = {
            row["name"]: row
            for row in self._scan_label(
                schema.PACKAGE,
                ("name", "ecosystem", "is_compromised", "risk_score", "advisories", "mal_versions"),
            )
            if row.get("name")
        }
        versions_by_package: dict[int, list[dict[str, Any]]] = {}
        for row in self._scan_label(
            schema.VERSION, ("semver", "package_id", "compromised_window")
        ):
            owner = row.get("package_id")
            if owner is not None and row.get("semver"):
                versions_by_package.setdefault(int(owner), []).append(row)

        by_package: dict[str, list[Advisory]] = {}
        for advisory in advisories:
            by_package.setdefault(advisory.package, []).append(advisory)

        node_rows: list[_NodeRow] = []
        newly_flagged = 0
        for name, package_advisories in by_package.items():
            row = packages.get(name)
            if row is None:
                continue  # not a package this namespace tracks
            pkg_id = int(row["id"])
            known_ids = {a for a in (row.get("advisories") or "").split(",") if a}
            new_ids = [a.id for a in package_advisories if a.id not in known_ids]
            malicious = any(
                a.malicious or a.id.startswith("MAL-") for a in package_advisories
            )
            props: dict[str, Any] = {
                "advisories": ",".join(sorted(known_ids | {a.id for a in package_advisories}))
            }
            if malicious:
                props["risk_score"] = RISK_MALICIOUS
                if not row.get("is_compromised"):
                    newly_flagged += 1
                props["is_compromised"] = True
                # The exact malicious releases, when the advisory enumerates
                # them. This is what lets bad-version resolution distinguish
                # the COMPROMISE (e.g. debug 4.4.2) from an old vulnerability
                # window (debug <=3.2.6) - the two produce opposite blast radii.
                known_mal = {v for v in (row.get("mal_versions") or "").split(",") if v}
                enumerated = {
                    v
                    for a in package_advisories
                    if a.malicious or a.id.startswith("MAL-")
                    for v in a.affected_versions
                }
                if known_mal | enumerated:
                    props["mal_versions"] = ",".join(sorted(known_mal | enumerated))
            elif new_ids:
                current = float(row.get("risk_score") or RISK_DEFAULT)
                props["risk_score"] = round(
                    min(RISK_CAP, current + RISK_BUMP * len(new_ids)), 4
                )
            node_rows.append(_NodeRow(schema.PACKAGE, pkg_id, props))

            for version in versions_by_package.get(pkg_id, ()):
                if version.get("compromised_window"):
                    continue  # only ever raised, never reset here
                if any(_version_affected(version["semver"], a) for a in package_advisories):
                    node_rows.append(
                        _NodeRow(schema.VERSION, int(version["id"]), {"compromised_window": True})
                    )

        if node_rows:
            self._write_nodes(node_rows)
        return newly_flagged

    # -- typosquats --------------------------------------------------------

    def _write_typosquats(self, candidate_names: Iterable[str]) -> int:
        """TYPOSQUAT_OF edges from ingested names to look-alike popular names.

        HydraDB has no string functions, so distance is computed here and
        stored on the edge; traversal later just reads it back. Pairs sharing
        a scope are skipped (`@types/react` is not squatting `react`), and the
        squatter must be the lower-download side of the pair.
        """
        packages = self._scan_label(schema.PACKAGE, ("name", "downloads_weekly"))
        downloads = {row["name"]: row.get("downloads_weekly") for row in packages if row.get("name")}
        id_of = {row["name"]: int(row["id"]) for row in packages if row.get("name")}
        popular = [
            name for name, dl in downloads.items()
            if dl is not None and dl >= TYPOSQUAT_MIN_DOWNLOADS
        ]

        edges: list[_EdgeRow] = []
        squatter_rows: list[_NodeRow] = []
        for candidate in candidate_names:
            if candidate not in id_of:
                continue
            candidate_norm = normalize_name(candidate)
            candidate_scope = _scope_of(candidate)
            candidate_downloads = downloads.get(candidate)
            for target in popular:
                if target == candidate:
                    continue
                if candidate_scope is not None and candidate_scope == _scope_of(target):
                    continue
                if candidate_downloads is not None and candidate_downloads >= (downloads[target] or 0):
                    continue  # the popular side of a pair is not the squatter
                target_norm = normalize_name(target)
                distance = levenshtein(candidate_norm, target_norm, cap=TYPOSQUAT_MAX_DISTANCE)
                if distance > TYPOSQUAT_MAX_DISTANCE:
                    continue
                longest = max(len(candidate_norm), len(target_norm)) or 1
                edges.append(_EdgeRow(
                    schema.TYPOSQUAT_OF,
                    id_of[candidate], schema.PACKAGE,
                    id_of[target], schema.PACKAGE,
                    {
                        "edit_distance": distance,
                        "similarity_score": round(1.0 - distance / longest, 4),
                    },
                ))
                squatter_rows.append(
                    _NodeRow(schema.PACKAGE, id_of[candidate], {"is_typosquat": True})
                )

        if edges:
            self._write_edges(edges)
            self._write_nodes(squatter_rows)
        return len(edges)
