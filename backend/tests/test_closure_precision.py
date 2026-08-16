"""Unit tests for app.closure_precision.refine against a stub client.

The fabricated graph exercises every rule in the module docstring:

    root R (tanstack-query, bad version 4.28.0)
      hop 1: P_sat   "~4.27.0"   provably excluded -> pruned (satellite)
             P_ok    "^4.27.0"   resolves 4.28.0   -> stays
             P_garb  garbage     fail open         -> stays
             P_dual  "~4.26.0"   hop-1 edge pruned, but also reachable via
                                 P_ok              -> stays (dual-path survivor)
             L_clean "^4.0.0"    resolves, but RESOLVED_IN pins 4.26.1
                                 (outside window)  -> pruned, and S2 with it
             L_bad   "=4.28.0"   RESOLVED_IN pins 4.28.0 (windowed) -> stays
      hop 2: P_satchild via P_sat only -> pruned with it
             S1 via P_ok -> stays; S2 via L_clean only -> pruned; S3 via L_bad

Runnable two ways:
  * pytest backend/tests/test_closure_precision.py
  * backend/.venv/bin/python -m pytest backend/tests/test_closure_precision.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.closure_precision import refine  # noqa: E402

# --- ids, one per schema partition ------------------------------------------

R = 1_000_004
P_SAT = 1_000_100
P_OK = 1_000_101
P_GARB = 1_000_102
P_DUAL = 1_000_103
P_SATCHILD = 1_000_104
V_CLEAN = 2_000_001  # 4.26.1, outside the window
V_BAD = 2_000_002  # 4.28.0, windowed
S1 = 4_000_001
S2 = 4_000_002
S3 = 4_000_003
S4 = 4_000_004  # never exposed; exists so totals differ from exposure
L_CLEAN = 5_000_001
L_BAD = 5_000_002

BAD = "4.28.0"


# --- stub client -------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self.rows = rows
        self.columns = list(rows[0]) if rows else []
        self.latency_ms = 1.0

    def scalar(self, column=None, default=None):
        if not self.rows:
            return default
        key = column if column is not None else (self.columns[0] if self.columns else None)
        value = self.rows[0].get(key, default)
        return default if value is None else value


class StubClient:
    """Routes each of refine()'s queries (and analytics.load_fleet's two) by
    the distinctive token in its text."""

    def __init__(self, *, hop1, versions, resolved, inverse):
        self.hop1 = hop1  # list[(dependent_id, constraint)]
        self.versions = versions  # list[(version_id, semver, windowed)]
        self.resolved = resolved  # list[(source_id, target_id)]
        self.inverse = inverse  # list[(source_id, target_id)] DEPENDED_ON_BY
        self.queries: list[str] = []

    def execute(self, query, parameters=None, timeout_ms=None):
        q = " ".join(query.split())
        self.queries.append(q)
        if "DEPENDED_ON_BY" in q:
            return _Result([{"source": s, "target": t} for s, t in self.inverse])
        if "RESOLVED_IN" in q:
            return _Result([{"source": s, "target": t} for s, t in self.resolved])
        if "DEPENDS_ON" in q:
            assert parameters == {"pkg": R}
            return _Result([{"id": i, "c": c} for i, c in self.hop1])
        if ":Version" in q:
            assert parameters == {"pkg": R}
            return _Result([{"id": i, "semver": sv, "w": w} for i, sv, w in self.versions])
        if ":Service" in q:  # analytics.load_fleet
            return _Result(
                [
                    {"id": S1, "name": "svc-one", "criticality": "tier-1"},
                    {"id": S2, "name": "svc-two", "criticality": "tier-2"},
                    {"id": S3, "name": "svc-three", "criticality": "tier-1"},
                    {"id": S4, "name": "svc-four", "criticality": "tier-3"},
                ]
            )
        if ":Lockfile" in q:  # analytics.load_fleet's count
            return _Result([{"c": 3}])
        raise AssertionError(f"unexpected query: {q}")


def _default_stub(**overrides):
    fields = dict(
        hop1=[
            (P_SAT, "~4.27.0"),
            (P_OK, "^4.27.0"),
            (P_GARB, "definitely !! not a range"),
            (P_DUAL, "~4.26.0"),
            (L_CLEAN, "^4.0.0"),
            (L_BAD, "=4.28.0"),
        ],
        versions=[(V_CLEAN, "4.26.1", False), (V_BAD, "4.28.0", True)],
        resolved=[
            (R, V_CLEAN),  # the package's own release history: must be ignored
            (R, V_BAD),
            (L_CLEAN, V_CLEAN),
            (L_BAD, V_BAD),
        ],
        inverse=[
            (R, P_SAT),
            (R, P_OK),
            (R, P_GARB),
            (R, P_DUAL),
            (R, L_CLEAN),
            (R, L_BAD),
            (P_SAT, P_SATCHILD),
            (P_OK, P_DUAL),
            (P_OK, S1),
            (L_CLEAN, S2),
            (L_BAD, S3),
        ],
    )
    fields.update(overrides)
    return StubClient(**fields)


def _node(node_id, label, name, **props):
    return {"id": node_id, "label": label, "name": name, **props}


def _closure():
    affected = [P_SAT, P_OK, P_GARB, P_DUAL, P_SATCHILD, S1, S2, S3, L_CLEAN, L_BAD]
    return {
        "root": _node(R, "Package", "tanstack-query"),
        "depth": 6,
        "latency_ms": 10.0,
        "affected_ids": sorted(affected),
        "affected_package_ids": [P_SAT, P_OK, P_GARB, P_DUAL, P_SATCHILD],
        "affected_version_ids": [],
        "affected_maintainer_ids": [],
        "affected_service_ids": [S1, S2, S3],
        "affected_lockfile_ids": [L_CLEAN, L_BAD],
        "affected_nodes": [
            _node(P_SAT, "Package", "vue-query-shim"),
            _node(P_OK, "Package", "query-adapter-core"),
            _node(P_GARB, "Package", "garbage-constraint-pkg"),
            _node(P_DUAL, "Package", "dual-path-pkg"),
            _node(P_SATCHILD, "Package", "satellite-child"),
            _node(S1, "Service", "svc-one", criticality="tier-1"),
            _node(S2, "Service", "svc-two", criticality="tier-2"),
            _node(S3, "Service", "svc-three", criticality="tier-1"),
            _node(L_CLEAN, "Lockfile", "svc-two/package-lock.json"),
            _node(L_BAD, "Lockfile", "svc-three/package-lock.json"),
        ],
        "paths": [
            [R, P_SAT],
            [R, P_OK],
            [R, P_GARB],
            [R, P_DUAL],
            [R, L_CLEAN],
            [R, L_BAD],
            [R, P_SAT, P_SATCHILD],
            [R, P_OK, P_DUAL],
            [R, P_OK, S1],
            [R, L_CLEAN, S2],
            [R, L_BAD, S3],
        ],
        "blast_radius": {
            "exposed_services": 3,
            "total_services": 4,
            "percentage": 75.0,
            "exposed_lockfiles": 2,
            "total_lockfiles": 3,
            "tier1_exposed": 2,
        },
    }


# --- tests -------------------------------------------------------------------


def test_pruned_satellite_and_its_child():
    """A '~4.27.0' satellite cannot resolve 4.28.0: it and everything only
    reachable through it leave the closure."""
    out = refine(_default_stub(), _closure(), BAD)
    precision = out["precision"]
    assert precision["mode"] == "version"
    assert precision["bad_version"] == BAD
    assert P_SAT in precision["pruned_package_ids"]
    assert P_SATCHILD in precision["pruned_package_ids"]
    assert P_SAT not in out["affected_package_ids"]
    assert P_SATCHILD not in out["affected_ids"]
    excluded = {e["id"]: e["constraint"] for e in precision["excluded_direct"]}
    assert excluded[P_SAT] == "~4.27.0"
    assert [R, P_SAT] not in out["paths"]
    assert [R, P_SAT, P_SATCHILD] not in out["paths"]


def test_dual_path_survivor_stays_exposed():
    """P_dual's hop-1 edge is pruned, but it is also reachable via P_ok: the
    node stays exposed while the pruned-edge path disappears."""
    out = refine(_default_stub(), _closure(), BAD)
    precision = out["precision"]
    assert P_DUAL in out["affected_package_ids"]
    assert P_DUAL not in precision["pruned_package_ids"]
    excluded = {e["id"]: e["constraint"] for e in precision["excluded_direct"]}
    assert excluded[P_DUAL] == "~4.26.0"  # the edge evidence is still reported
    assert [R, P_DUAL] not in out["paths"]  # hop-1 edge pruned
    assert [R, P_OK, P_DUAL] in out["paths"]  # the surviving route remains


def test_fail_open_on_garbage_constraint():
    """An unparseable constraint must count as exposed, never as pruned."""
    out = refine(_default_stub(), _closure(), BAD)
    assert P_GARB in out["affected_package_ids"]
    assert P_GARB not in out["precision"]["pruned_package_ids"]
    assert P_GARB not in {e["id"] for e in out["precision"]["excluded_direct"]}
    assert [R, P_GARB] in out["paths"]


def test_missing_constraint_fails_open():
    stub = _default_stub(
        hop1=[
            (P_SAT, "~4.27.0"),
            (P_OK, "^4.27.0"),
            (P_GARB, None),  # missing entirely
            (P_DUAL, "~4.26.0"),
            (L_CLEAN, "^4.0.0"),
            (L_BAD, "=4.28.0"),
        ]
    )
    out = refine(stub, _closure(), BAD)
    assert P_GARB in out["affected_package_ids"]


def test_clean_pinned_lockfile_override():
    """L_clean is package-reachable and its hop-1 range resolves 4.28.0, but
    its RESOLVED_IN pin is 4.26.1 - outside the window - so it is pruned, and
    S2, whose only exposure ran through it, goes with it. L_bad pins the
    windowed 4.28.0 and stays, keeping S3 exposed."""
    out = refine(_default_stub(), _closure(), BAD)
    precision = out["precision"]
    assert precision["pruned_lockfile_ids"] == [L_CLEAN]
    assert precision["pruned_service_ids"] == [S2]
    assert out["affected_lockfile_ids"] == [L_BAD]
    assert sorted(out["affected_service_ids"]) == [S1, S3]
    assert [R, L_CLEAN] not in out["paths"]
    assert [R, L_CLEAN, S2] not in out["paths"]
    assert [R, L_BAD, S3] in out["paths"]


def test_mixed_pins_windowed_wins():
    """Strongest evidence wins among one lockfile's own pins: a lockfile that
    resolves the package at BOTH a clean and a windowed release (a real
    package-lock can hold several copies) is never treated as clean."""
    stub = _default_stub(
        resolved=[
            (R, V_CLEAN),
            (R, V_BAD),
            (L_CLEAN, V_CLEAN),
            (L_BAD, V_CLEAN),  # a clean copy too - the windowed one must win
            (L_BAD, V_BAD),
        ]
    )
    out = refine(stub, _closure(), BAD)
    assert L_BAD in out["affected_lockfile_ids"]
    assert S3 in out["affected_service_ids"]
    assert out["precision"]["pruned_lockfile_ids"] == [L_CLEAN]


def test_hop1_gate_prunes_an_unreachable_windowed_pin():
    """The windowed-pin rule keeps a lockfile exposed only while it is still
    package-reachable: when its sole hop-1 edge is provably gated (the
    radix/live debug topology), it is pruned along with its service."""
    stub = _default_stub(
        hop1=[
            (P_SAT, "~4.27.0"),
            (P_OK, "^4.27.0"),
            (P_GARB, "definitely !! not a range"),
            (P_DUAL, "~4.26.0"),
            (L_CLEAN, "^4.0.0"),
            (L_BAD, "~1.0.0"),  # cannot resolve 4.28.0 - transmission gated
        ]
    )
    out = refine(stub, _closure(), BAD)
    assert L_BAD not in out["affected_lockfile_ids"]
    assert S3 not in out["affected_service_ids"]
    assert L_BAD in {e["id"] for e in out["precision"]["excluded_direct"]}
    assert [R, L_BAD, S3] not in out["paths"]


def test_blast_radius_recomputed_and_package_level_kept():
    out = refine(_default_stub(), _closure(), BAD)
    blast = out["blast_radius"]
    assert blast["exposed_services"] == 2  # S1, S3
    assert blast["total_services"] == 4
    assert blast["percentage"] == 50.0
    assert blast["exposed_lockfiles"] == 1
    assert blast["total_lockfiles"] == 3
    assert blast["tier1_exposed"] == 2  # both survivors are tier-1
    assert out["precision"]["package_level"] == {"exposed_services": 3, "percentage": 75.0}


def test_bad_version_none_is_package_mode():
    """No version, no pruning: the closure passes through untouched."""
    stub = _default_stub()
    original = _closure()
    expected = copy.deepcopy(original)
    out = refine(stub, original, None)
    precision = out.pop("precision")
    assert out == expected  # nothing else changed, latency included
    assert precision == {
        "mode": "package",
        "bad_version": None,
        "excluded_direct": [],
        "pruned_package_ids": [],
        "pruned_service_ids": [],
        "pruned_lockfile_ids": [],
        "package_level": {"exposed_services": 3, "percentage": 75.0},
    }
    assert stub.queries == []  # fail-open path never touches the graph


def test_unparseable_bad_version_fails_open_to_package_mode():
    """A bad_version that does not parse would make satisfies() reject every
    constraint and empty the closure - so it must fail open instead."""
    stub = _default_stub()
    out = refine(stub, _closure(), "not.a.version!!")
    assert out["precision"]["mode"] == "package"
    assert out["precision"]["pruned_package_ids"] == []
    assert out["affected_service_ids"] == [S1, S2, S3]
    assert stub.queries == []


def test_affected_nodes_pruned_with_the_ids():
    out = refine(_default_stub(), _closure(), BAD)
    surviving = {n["id"] for n in out["affected_nodes"]}
    assert surviving == set(out["affected_ids"])
    assert P_SAT not in surviving
    assert L_CLEAN not in surviving


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
