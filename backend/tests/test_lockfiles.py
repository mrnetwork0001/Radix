"""Verification for lockfile parsing (lockfiles.py) and repo scanning (repo.py).

Runs under pytest, and also directly without it:

    backend/.venv/bin/python backend/tests/test_lockfiles.py

The frontend package-lock.json test runs against the real lockfile in this
repo; expectations there are re-derived from the raw JSON so the test stays
valid when the frontend's dependencies move.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ingest import discover_lockfiles, parse_lockfile, scan_source  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures"
FRONTEND_LOCK = REPO_ROOT / "frontend" / "package-lock.json"
FRONTEND_MANIFEST = REPO_ROOT / "frontend" / "package.json"


def _name_from_lock_path(path_key: str) -> str:
    # Independent re-derivation for cross-checking the parser.
    marker = "node_modules/"
    idx = path_key.rfind(marker)
    return path_key[idx + len(marker):] if idx != -1 else path_key


# --------------------------------------------------------------------------
# 1. The real npm v3 lockfile
# --------------------------------------------------------------------------


def test_frontend_npm_v3_releases_and_edges():
    parsed = parse_lockfile(FRONTEND_LOCK, REPO_ROOT)
    raw = json.loads(FRONTEND_LOCK.read_text())
    packages = raw["packages"]

    assert parsed.kind == "npm"
    assert parsed.path == "frontend/package-lock.json"
    assert parsed.root_name == "radix-frontend"

    # Every non-root entry survives, deduped by (name, version).
    expected = {
        (_name_from_lock_path(k), v["version"]) for k, v in packages.items() if k
    }
    got = {(r.name, r.version) for r in parsed.releases}
    assert got == expected
    assert len(parsed.edges) > len(parsed.releases)  # real graphs are edge-heavy


def test_frontend_spot_check_versions():
    parsed = parse_lockfile(FRONTEND_LOCK, REPO_ROOT)
    raw = json.loads(FRONTEND_LOCK.read_text())
    packages = raw["packages"]
    releases = {(r.name, r.version): r for r in parsed.releases}

    for name, major, dev in (
        ("react", "18", False),
        ("react-force-graph-2d", "1", False),
        ("vite", "7", True),
    ):
        version = packages[f"node_modules/{name}"]["version"]
        assert version.split(".")[0] == major, f"{name} resolved to implausible {version}"
        release = releases[(name, version)]
        assert release.dev is dev
        assert release.direct is True
        assert release.integrity and release.integrity.startswith("sha")


def test_frontend_root_edges_carry_manifest_ranges():
    parsed = parse_lockfile(FRONTEND_LOCK, REPO_ROOT)
    manifest = json.loads(FRONTEND_MANIFEST.read_text())
    declared = dict(manifest.get("dependencies") or {})
    declared_dev = dict(manifest.get("devDependencies") or {})

    root_edges = {e.dst_name: e for e in parsed.edges if e.src_name is None}
    assert set(root_edges) == set(declared) | set(declared_dev)
    for name, rng in declared.items():
        assert root_edges[name].constraint == rng
        assert root_edges[name].dev is False
    for name, rng in declared_dev.items():
        assert root_edges[name].constraint == rng
        assert root_edges[name].dev is True
    # The exact ranges the task names:
    assert root_edges["react"].constraint == "^18.3.1"
    assert root_edges["vite"].constraint == "^7.3.6"

    # direct=true only for root-manifest deps.
    direct = {(r.name, r.version) for r in parsed.releases if r.direct}
    assert direct == {(e.dst_name, e.dst_version) for e in root_edges.values()}


def test_frontend_nested_node_modules_resolution():
    """Nearest enclosing node_modules wins: vite pins its own fdir/picomatch."""
    parsed = parse_lockfile(FRONTEND_LOCK, REPO_ROOT)
    raw = json.loads(FRONTEND_LOCK.read_text())
    packages = raw["packages"]

    vite_version = packages["node_modules/vite"]["version"]
    for dep in ("fdir", "picomatch"):
        nested = packages[f"node_modules/vite/node_modules/{dep}"]["version"]
        edge = next(
            e
            for e in parsed.edges
            if e.src_name == "vite" and e.src_version == vite_version and e.dst_name == dep
        )
        assert edge.dst_version == nested, (
            f"vite->{dep} resolved {edge.dst_version}, lockfile nests {nested}"
        )


# --------------------------------------------------------------------------
# 2. yarn.lock v1 fixture
# --------------------------------------------------------------------------


def test_yarn_v1_fixture():
    root = FIXTURES / "yarn_project"
    parsed = parse_lockfile(root / "yarn.lock", root)
    assert parsed.kind == "yarn"
    assert parsed.root_name == "yarn-fixture"

    releases = {(r.name, r.version): r for r in parsed.releases}
    assert set(releases) == {
        ("@scope/demo", "1.0.2"),
        ("debug", "4.3.4"),
        ("left-pad", "1.3.0"),
        ("ms", "2.1.2"),
        ("ms", "2.1.3"),
    }

    edges = {(e.src_name, e.src_version, e.dst_name, e.constraint, e.dst_version, e.dev) for e in parsed.edges}
    assert edges == {
        (None, None, "@scope/demo", "~1.0.0", "1.0.2", False),
        (None, None, "left-pad", "^1.3.0", "1.3.0", False),
        (None, None, "ms", "^2.1.1", "2.1.3", False),
        (None, None, "debug", "^4.3.4", "4.3.4", True),
        # one lockfile entry serves both left-pad ranges
        ("@scope/demo", "1.0.2", "left-pad", "~1.3.0", "1.3.0", False),
        # per-dependent resolution: debug pins ms 2.1.2 while the root gets 2.1.3
        ("debug", "4.3.4", "ms", "2.1.2", "2.1.2", True),
    }

    # dev derived by reachability: only the devDependencies subtree is dev.
    assert releases[("debug", "4.3.4")].dev is True
    assert releases[("ms", "2.1.2")].dev is True
    assert releases[("ms", "2.1.3")].dev is False
    assert releases[("left-pad", "1.3.0")].dev is False
    assert {k for k, r in releases.items() if r.direct} == {
        ("@scope/demo", "1.0.2"),
        ("left-pad", "1.3.0"),
        ("ms", "2.1.3"),
        ("debug", "4.3.4"),
    }
    assert releases[("ms", "2.1.3")].resolved_url.endswith("ms-2.1.3.tgz#574c8138ce1d2b5861f0b44579dbadd60c78e73a")


# --------------------------------------------------------------------------
# 2b. pnpm-lock.yaml fixtures (v9 and v6 key styles)
# --------------------------------------------------------------------------


def test_pnpm_v9_fixture():
    root = FIXTURES / "pnpm_v9_project"
    parsed = parse_lockfile(root / "pnpm-lock.yaml", root)
    assert parsed.kind == "pnpm"
    assert parsed.root_name == "pnpm-v9-fixture"

    releases = {(r.name, r.version): r for r in parsed.releases}
    assert set(releases) == {
        ("@scope/demo", "1.0.2"),
        ("debug", "4.3.7"),
        ("ms", "2.0.0"),
        ("ms", "2.1.3"),
    }

    edges = {(e.src_name, e.src_version, e.dst_name, e.constraint, e.dst_version, e.dev) for e in parsed.edges}
    assert edges == {
        # importer edges keep the manifest specifier; peer suffix is stripped
        (None, None, "@scope/demo", "~1.0.0", "1.0.2", False),
        (None, None, "ms", "^2.1.1", "2.1.3", False),
        (None, None, "debug", "^4.3.4", "4.3.7", True),
        # snapshot edges: exact pins, source key carries a peer suffix
        ("@scope/demo", "1.0.2", "ms", "2.1.3", "2.1.3", False),
        ("debug", "4.3.7", "ms", "2.0.0", "2.0.0", True),
    }

    assert releases[("debug", "4.3.7")].dev is True  # v9 has no dev field: reachability
    assert releases[("ms", "2.0.0")].dev is True
    assert releases[("ms", "2.1.3")].dev is False
    assert {k for k, r in releases.items() if r.direct} == {
        ("@scope/demo", "1.0.2"),
        ("ms", "2.1.3"),
        ("debug", "4.3.7"),
    }
    assert releases[("ms", "2.1.3")].integrity.startswith("sha512-6Flzub")


def test_pnpm_v6_fixture():
    root = FIXTURES / "pnpm_v6_project"
    parsed = parse_lockfile(root / "pnpm-lock.yaml", root)
    assert parsed.kind == "pnpm"

    releases = {(r.name, r.version): r for r in parsed.releases}
    assert set(releases) == {("debug", "4.3.7"), ("ms", "2.0.0"), ("ms", "2.1.3")}

    edges = {(e.src_name, e.src_version, e.dst_name, e.constraint, e.dst_version, e.dev) for e in parsed.edges}
    assert edges == {
        (None, None, "ms", "^2.1.1", "2.1.3", False),
        (None, None, "debug", "^4.3.4", "4.3.7", True),
        ("debug", "4.3.7", "ms", "2.0.0", "2.0.0", True),
    }

    # v6 records dev on the package entries themselves (/pkg@ver key style).
    assert releases[("debug", "4.3.7")].dev is True
    assert releases[("ms", "2.0.0")].dev is True
    assert releases[("ms", "2.1.3")].dev is False


# --------------------------------------------------------------------------
# 2c. package-lock.json v1 fixture (regression for the nested-tree dialect)
# --------------------------------------------------------------------------


def test_npm_v1_fixture():
    root = FIXTURES / "npm_v1_project"
    parsed = parse_lockfile(root / "package-lock.json", root)
    assert parsed.kind == "npm"
    assert parsed.root_name == "npm-v1-fixture"

    releases = {(r.name, r.version): r for r in parsed.releases}
    assert set(releases) == {("debug", "4.3.4"), ("ms", "2.1.2"), ("ms", "2.1.3")}
    assert releases[("debug", "4.3.4")].dev is True
    assert releases[("ms", "2.1.2")].dev is True
    assert releases[("ms", "2.1.3")].dev is False

    edges = {(e.src_name, e.src_version, e.dst_name, e.constraint, e.dst_version, e.dev) for e in parsed.edges}
    assert edges == {
        (None, None, "ms", "^2.1.1", "2.1.3", False),
        (None, None, "debug", "^4.3.4", "4.3.4", True),
        # nearest enclosing scope: debug's nested ms 2.1.2 beats top-level 2.1.3
        ("debug", "4.3.4", "ms", "2.1.2", "2.1.2", True),
    }


# --------------------------------------------------------------------------
# 3. scan_source on this repo
# --------------------------------------------------------------------------


def test_scan_source_local_repo():
    scan = scan_source(str(REPO_ROOT))
    assert scan.repo_name == REPO_ROOT.name
    assert scan.source == str(REPO_ROOT)

    paths = {lf.path for lf in scan.lockfiles}
    assert "frontend/package-lock.json" in paths
    assert not any("node_modules" in p for p in paths)

    assert scan.commit_hash is not None
    assert len(scan.commit_hash) == 40
    assert all(c in "0123456789abcdef" for c in scan.commit_hash)

    frontend = next(lf for lf in scan.lockfiles if lf.path == "frontend/package-lock.json")
    assert frontend.releases and frontend.edges


def test_discover_skips_node_modules(tmp_path=None):
    # Plain-python friendly: build the tree in a scratch dir without pytest's tmp_path.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "node_modules" / "dep").mkdir(parents=True)
        (root / "node_modules" / "dep" / "package-lock.json").write_text("{}")
        (root / "app").mkdir()
        (root / "app" / "yarn.lock").write_text("# yarn lockfile v1\n")
        found = discover_lockfiles(root)
        assert [p.relative_to(root).as_posix() for p in found] == ["app/yarn.lock"]


# --------------------------------------------------------------------------
# Plain-python runner (no pytest required)
# --------------------------------------------------------------------------


def _report_frontend() -> None:
    parsed = parse_lockfile(FRONTEND_LOCK, REPO_ROOT)
    dev = sum(1 for r in parsed.releases if r.dev)
    direct = sum(1 for r in parsed.releases if r.direct)
    root_edges = [e for e in parsed.edges if e.src_name is None]
    releases = {(r.name, r.version): r for r in parsed.releases}

    print("--- frontend/package-lock.json (real npm v3) ---")
    print(f"releases          : {len(parsed.releases)}")
    print(f"edges             : {len(parsed.edges)} ({len(root_edges)} from root manifest)")
    print(f"direct deps       : {direct}")
    print(f"dev split         : {dev} dev / {len(parsed.releases) - dev} prod")
    for name in ("react", "react-dom", "react-force-graph-2d", "vite", "typescript"):
        match = [r for (n, _), r in releases.items() if n == name]
        for r in match:
            print(f"  {r.name}@{r.version}  dev={r.dev} direct={r.direct}")
    for e in root_edges:
        if e.dst_name in ("react", "react-force-graph-2d", "vite"):
            print(f"  root --[{e.constraint}]--> {e.dst_name}@{e.dst_version} dev={e.dev}")


def _report_scan() -> None:
    scan = scan_source(str(REPO_ROOT))
    print("--- scan_source(REPO_ROOT) ---")
    print(f"repo_name   : {scan.repo_name}")
    print(f"repo_url    : {scan.repo_url}")
    print(f"commit_hash : {scan.commit_hash}")
    print(f"lockfiles   : {[lf.path for lf in scan.lockfiles]}")


if __name__ == "__main__":
    tests = [
        test_frontend_npm_v3_releases_and_edges,
        test_frontend_spot_check_versions,
        test_frontend_root_edges_carry_manifest_ranges,
        test_frontend_nested_node_modules_resolution,
        test_yarn_v1_fixture,
        test_pnpm_v9_fixture,
        test_pnpm_v6_fixture,
        test_npm_v1_fixture,
        test_scan_source_local_repo,
        test_discover_skips_node_modules,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception:
            failures += 1
            print(f"FAIL  {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS  {test.__name__}")
    print()
    _report_frontend()
    _report_scan()
    print()
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
