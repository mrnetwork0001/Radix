"""Graph schema for Radix.

HydraDB keys every node by a non-negative integer ``id``, so names live in
properties and this module owns the integer space. Ids are partitioned by node
type, which makes a raw id self-describing: 3_000_004 is a Maintainer, always.
That property is what lets the API classify traversal results without a second
round trip, since a variable-length ``MATCH`` returns bare ``vertex_id`` values.

See ``docs/HYDRADB_CONTRACT.md`` for the verified engine constraints that shape
this design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

# --- Node labels -----------------------------------------------------------
# HydraDB requires exactly one label per endpoint in a batched edge write, so
# every node carries precisely one of these.

PACKAGE = "Package"
VERSION = "Version"
MAINTAINER = "Maintainer"
SERVICE = "Service"
LOCKFILE = "Lockfile"

NODE_LABELS = (PACKAGE, VERSION, MAINTAINER, SERVICE, LOCKFILE)

# --- Edge types ------------------------------------------------------------

DEPENDS_ON = "DEPENDS_ON"
MAINTAINED_BY = "MAINTAINED_BY"
RESOLVED_IN = "RESOLVED_IN"
TYPOSQUAT_OF = "TYPOSQUAT_OF"

#: Materialised inverse of :data:`DEPENDS_ON`.
#:
#: HydraDB rejects target-anchored variable-length traversal ("variable-length
#: MATCH requires a fixed source id"), but reverse transitive closure — who
#: depends on the compromised package — is exactly that shape. Mirroring every
#: dependency edge turns the reverse question into a forward traversal.
DEPENDED_ON_BY = "DEPENDED_ON_BY"

#: Inverse of :data:`MAINTAINED_BY`, so the maintainer subgraph can be walked
#: outward from a maintainer to their other packages.
MAINTAINS = "MAINTAINS"

EDGE_TYPES = (
    DEPENDS_ON,
    DEPENDED_ON_BY,
    MAINTAINED_BY,
    MAINTAINS,
    RESOLVED_IN,
    TYPOSQUAT_OF,
)

# --- Integer id space ------------------------------------------------------

_BASES = {
    PACKAGE: 1_000_000,
    VERSION: 2_000_000,
    MAINTAINER: 3_000_000,
    SERVICE: 4_000_000,
    LOCKFILE: 5_000_000,
}

#: Relationship ids share one space. A batched edge write carrying properties
#: must supply an explicit ``id: row.<field>``, so the seeder allocates from here.
EDGE_ID_BASE = 9_000_000

_SPAN = 1_000_000


def label_for_id(node_id: int) -> str | None:
    """Recover a node's label from its integer id."""
    for label, base in _BASES.items():
        if base <= node_id < base + _SPAN:
            return label
    return None


@dataclass
class IdSpace:
    """Allocates stable integer ids from natural keys.

    Allocation is sequential within each label's partition and memoised by key,
    so re-seeding the same ecosystem reproduces the same graph and ``MERGE``
    stays genuinely idempotent.
    """

    _by_key: dict[tuple[str, str], int] = field(default_factory=dict)
    _next: dict[str, int] = field(default_factory=dict)
    _next_edge: int = EDGE_ID_BASE

    def node(self, label: str, key: str) -> int:
        """Return the id for ``key`` within ``label``, allocating on first use."""
        if label not in _BASES:
            raise ValueError(f"unknown node label: {label!r}")
        cache_key = (label, key)
        if cache_key not in self._by_key:
            offset = self._next.get(label, 0)
            self._next[label] = offset + 1
            self._by_key[cache_key] = _BASES[label] + offset
        return self._by_key[cache_key]

    def edge(self) -> int:
        """Allocate a fresh relationship id."""
        edge_id = self._next_edge
        self._next_edge += 1
        return edge_id

    def keys_for(self, label: str) -> Iterator[tuple[str, int]]:
        for (lbl, key), node_id in self._by_key.items():
            if lbl == label:
                yield key, node_id

    def __len__(self) -> int:
        return len(self._by_key)


# --- Natural keys ----------------------------------------------------------
# Canonical string forms hashed into the id space. Keeping them in one place
# stops the seeder and the API from disagreeing about what names a node.


def package_key(ecosystem: str, name: str) -> str:
    return f"{ecosystem}:{name}"


def version_key(ecosystem: str, name: str, semver: str) -> str:
    return f"{ecosystem}:{name}@{semver}"


def maintainer_key(username: str) -> str:
    return f"maintainer:{username}"


def service_key(name: str) -> str:
    return f"service:{name}"


def lockfile_key(service: str, filename: str) -> str:
    return f"lockfile:{service}/{filename}"
