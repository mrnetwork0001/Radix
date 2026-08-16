"""Lockfile discovery and parsing into the ingestion IR.

Covers the four lockfile dialects that dominate real JS repos:

* ``package-lock.json`` v2/v3 - the flat ``packages`` map, resolved with npm's
  nearest-enclosing-``node_modules`` rule;
* ``package-lock.json`` v1 - the nested ``dependencies`` tree with ``requires``;
* ``yarn.lock`` v1 - yarn's custom block format, parsed here directly;
* ``pnpm-lock.yaml`` v6/v9 - ``importers`` for root edges, ``packages`` /
  ``snapshots`` for releases and edges, both key styles (``/pkg@ver`` and
  ``pkg@ver``).

Parsers are pure - no network, no HydraDB - and normalise everything into the
frozen IR in :mod:`app.ingest.model`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import yaml

from .model import DepEdge, PackageRelease, ParsedLockfile

__all__ = ["discover_lockfiles", "parse_lockfile", "LOCKFILE_NAMES"]

LOCKFILE_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")


def _skip_dir(name: str) -> bool:
    # node_modules is the contract's explicit exclusion; hidden directories
    # cover .git, .venv and tool caches, none of which hold project lockfiles.
    return name == "node_modules" or name.startswith(".")


def discover_lockfiles(root: Path) -> list[Path]:
    """Every recognised lockfile under ``root``, skipping node_modules and hidden dirs."""
    root = Path(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for name in LOCKFILE_NAMES:
            if name in filenames:
                found.append(Path(dirpath) / name)
    return sorted(found)


# --------------------------------------------------------------------------
# Shared accumulator
# --------------------------------------------------------------------------


class _Accumulator:
    """Collects releases and edges, deduping per the contract.

    Releases dedupe by ``(name, version)``: any prod occurrence clears ``dev``
    (dev means *only* reachable via devDependencies) and any direct occurrence
    sets ``direct``. Edges dedupe on their full identity with the same
    prod-wins rule for ``dev``.
    """

    def __init__(self) -> None:
        self._releases: dict[tuple[str, str], dict[str, Any]] = {}
        self._edges: dict[tuple[str | None, str | None, str, str, str], dict[str, Any]] = {}

    def add_release(
        self,
        name: str,
        version: str,
        *,
        dev: bool = False,
        direct: bool = False,
        resolved_url: str | None = None,
        integrity: str | None = None,
    ) -> None:
        rec = self._releases.get((name, version))
        if rec is None:
            self._releases[(name, version)] = {
                "dev": bool(dev),
                "direct": bool(direct),
                "resolved_url": resolved_url,
                "integrity": integrity,
            }
            return
        rec["dev"] = rec["dev"] and bool(dev)
        rec["direct"] = rec["direct"] or bool(direct)
        rec["resolved_url"] = rec["resolved_url"] or resolved_url
        rec["integrity"] = rec["integrity"] or integrity

    def mark_direct(self, name: str, version: str) -> None:
        rec = self._releases.get((name, version))
        if rec is not None:
            rec["direct"] = True

    def add_edge(
        self,
        src_name: str | None,
        src_version: str | None,
        dst_name: str,
        constraint: str,
        dst_version: str,
        *,
        dev: bool = False,
    ) -> None:
        key = (src_name, src_version, dst_name, constraint, dst_version)
        rec = self._edges.get(key)
        if rec is None:
            self._edges[key] = {"dev": bool(dev)}
        else:
            rec["dev"] = rec["dev"] and bool(dev)

    def apply_dev_reachability(self) -> None:
        """Derive dev flags for formats that do not record them (yarn, pnpm v9).

        A release is dev when it is reachable from the root's devDependencies
        but not from its prod/optional dependencies; edges out of a dev-only
        release become dev too.
        """
        adjacency: dict[tuple[str, str], list[tuple[str, str]]] = {}
        prod_roots: list[tuple[str, str]] = []
        dev_roots: list[tuple[str, str]] = []
        for (src_name, src_version, dst_name, _, dst_version), rec in self._edges.items():
            dst = (dst_name, dst_version)
            if src_name is None:
                (dev_roots if rec["dev"] else prod_roots).append(dst)
            else:
                adjacency.setdefault((src_name, src_version or ""), []).append(dst)

        def closure(roots: list[tuple[str, str]]) -> set[tuple[str, str]]:
            seen: set[tuple[str, str]] = set()
            stack = list(roots)
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(adjacency.get(node, ()))
            return seen

        prod = closure(prod_roots)
        dev_only = closure(dev_roots) - prod
        for key, rec in self._releases.items():
            if key in dev_only:
                rec["dev"] = True
        for (src_name, src_version, *_), rec in self._edges.items():
            if src_name is not None and (src_name, src_version or "") in dev_only:
                rec["dev"] = True

    def build(self) -> tuple[list[PackageRelease], list[DepEdge]]:
        releases = [
            PackageRelease(
                name=name,
                version=version,
                dev=rec["dev"],
                direct=rec["direct"],
                resolved_url=rec["resolved_url"],
                integrity=rec["integrity"],
            )
            for (name, version), rec in sorted(self._releases.items())
        ]
        edges = [
            DepEdge(
                src_name=key[0],
                src_version=key[1],
                dst_name=key[2],
                constraint=key[3],
                dst_version=key[4],
                dev=rec["dev"],
            )
            for key, rec in sorted(
                self._edges.items(),
                key=lambda item: (item[0][0] or "", item[0][1] or "", *item[0][2:]),
            )
        ]
        return releases, edges


def _root_edges_from_manifest(
    manifest: dict[str, Any],
    resolve: Callable[[str, str], str | None],
    acc: _Accumulator,
) -> None:
    """Root edges for formats whose lockfile omits the manifest (v1, yarn)."""
    for section, dev in (
        ("dependencies", False),
        ("optionalDependencies", False),
        ("devDependencies", True),
    ):
        for dep, constraint in (manifest.get(section) or {}).items():
            version = resolve(dep, str(constraint))
            if version is None:
                continue
            acc.add_edge(None, None, dep, str(constraint), version, dev=dev)
            acc.mark_direct(dep, version)


# --------------------------------------------------------------------------
# package-lock.json v2/v3
# --------------------------------------------------------------------------


def _npm_path_name(path_key: str) -> str:
    """Real package name from a ``packages`` key: last node_modules segment.

    ``node_modules/a/node_modules/@scope/b`` -> ``@scope/b``. Workspace keys
    without node_modules fall back to their last path segment.
    """
    marker = "node_modules/"
    idx = path_key.rfind(marker)
    if idx != -1:
        return path_key[idx + len(marker):]
    return path_key.rsplit("/", 1)[-1]


def _npm_resolve(packages: dict[str, dict], from_path: str, dep: str) -> dict | None:
    """npm's install-time resolution: the nearest enclosing node_modules wins."""
    path = from_path
    while True:
        candidate = f"{path}/node_modules/{dep}" if path else f"node_modules/{dep}"
        entry = packages.get(candidate)
        if entry is not None:
            if entry.get("link"):
                entry = packages.get(entry.get("resolved") or "")
            if entry is not None and entry.get("version"):
                return entry
        if not path:
            return None
        idx = path.rfind("/node_modules/")
        path = path[:idx] if idx != -1 else ""


def _parse_npm_new(data: dict[str, Any], acc: _Accumulator) -> None:
    packages: dict[str, dict] = data.get("packages") or {}

    for path_key, entry in packages.items():
        if not path_key or not isinstance(entry, dict) or entry.get("link"):
            continue
        name = entry.get("name") or _npm_path_name(path_key)
        version = entry.get("version")
        if not name or not version:
            continue
        acc.add_release(
            name,
            str(version),
            dev=bool(entry.get("dev")),
            resolved_url=entry.get("resolved"),
            integrity=entry.get("integrity"),
        )

    for path_key, entry in packages.items():
        if not isinstance(entry, dict) or entry.get("link"):
            continue
        is_root = path_key == ""
        # Workspace entries (a path without node_modules) are manifests too:
        # their devDependencies are installed, unlike an ordinary package's.
        is_manifest = is_root or "node_modules" not in path_key
        if is_root:
            src_name: str | None = None
            src_version: str | None = None
        else:
            src_name = entry.get("name") or _npm_path_name(path_key)
            src_version = entry.get("version")
            if not src_version:
                continue
        entry_dev = bool(entry.get("dev")) and not is_manifest
        sections = [("dependencies", entry_dev), ("optionalDependencies", entry_dev)]
        if is_manifest:
            sections.append(("devDependencies", True))
        for section, dev in sections:
            for dep, constraint in (entry.get(section) or {}).items():
                resolved = _npm_resolve(packages, path_key, dep)
                if resolved is None:
                    continue  # optional/platform-skipped dep: not installed
                dst_name = resolved.get("name") or dep
                dst_version = str(resolved["version"])
                acc.add_edge(src_name, src_version, dst_name, str(constraint), dst_version, dev=dev)
                if is_root:
                    acc.mark_direct(dst_name, dst_version)


# --------------------------------------------------------------------------
# package-lock.json v1
# --------------------------------------------------------------------------


def _v1_lookup(scopes: list[dict[str, dict]], dep: str) -> str | None:
    # Innermost scope first: v1 nesting mirrors the physical node_modules tree.
    for scope in reversed(scopes):
        entry = scope.get(dep)
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    return None


def _parse_npm_v1(data: dict[str, Any], manifest: dict[str, Any], acc: _Accumulator) -> None:
    top: dict[str, dict] = data.get("dependencies") or {}

    def walk(name: str, entry: dict, scopes: list[dict[str, dict]]) -> None:
        version = str(entry.get("version") or "")
        if not version or version.startswith(("npm:", "file:", "link:")):
            return
        dev = bool(entry.get("dev"))
        acc.add_release(
            name,
            version,
            dev=dev,
            resolved_url=entry.get("resolved"),
            integrity=entry.get("integrity"),
        )
        children: dict[str, dict] = entry.get("dependencies") or {}
        lookup = scopes + [children]
        for dep, constraint in (entry.get("requires") or {}).items():
            resolved_version = _v1_lookup(lookup, dep)
            if resolved_version is not None:
                acc.add_edge(name, version, dep, str(constraint), resolved_version, dev=dev)
        for child_name, child in children.items():
            if isinstance(child, dict):
                walk(child_name, child, lookup)

    for name, entry in top.items():
        if isinstance(entry, dict):
            walk(name, entry, [top])

    # v1 lockfiles do not repeat the manifest's ranges, so root edges come from
    # the sibling package.json when it exists.
    _root_edges_from_manifest(manifest, lambda dep, _rng: _v1_lookup([top], dep), acc)


# --------------------------------------------------------------------------
# yarn.lock v1
# --------------------------------------------------------------------------


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _yarn_selector(selector: str) -> tuple[str, str]:
    # The first '@' after position 0 splits name from range; position 0 is the
    # scope marker for "@scope/pkg" names, and alias ranges like "npm:x@^1"
    # stay intact on the range side.
    at = selector.find("@", 1)
    if at == -1:
        return selector, "*"
    return selector[:at], selector[at + 1:]


def _yarn_dep_line(line: str) -> tuple[str | None, str]:
    # Either `lodash "^4.17.20"` or `"@scope/x" "^1.0.0"`.
    if line.startswith('"'):
        end = line.find('"', 1)
        if end == -1:
            return None, ""
        name, rest = line[1:end], line[end + 1:].strip()
    else:
        name, _, rest = line.partition(" ")
        rest = rest.strip()
    return (name or None), _unquote(rest)


def _parse_yarn_blocks(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    mode: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            current, mode = None, None
            if line.endswith(":"):
                selectors = [
                    _yarn_selector(_unquote(part.strip()))
                    for part in line[:-1].split(",")
                    if part.strip()
                ]
                current = {"selectors": selectors, "fields": {}, "deps": {}, "optional": {}}
                entries.append(current)
        elif current is None:
            continue
        elif indent == 2:
            if line == "dependencies:":
                mode = "deps"
            elif line == "optionalDependencies:":
                mode = "optional"
            else:
                mode = None
                key, _, value = line.partition(" ")
                current["fields"][key] = _unquote(value.strip())
        elif indent >= 4 and mode is not None:
            dep, constraint = _yarn_dep_line(line)
            if dep:
                current[mode][dep] = constraint
    return entries


def _parse_yarn(text: str, manifest: dict[str, Any], acc: _Accumulator) -> None:
    entries = _parse_yarn_blocks(text)

    # One block serves several ranges; the selector index is the resolver.
    by_selector: dict[tuple[str, str], str] = {}
    for entry in entries:
        version = entry["fields"].get("version")
        if not version:
            continue
        for sel_name, sel_range in entry["selectors"]:
            by_selector[(sel_name, sel_range)] = version

    for entry in entries:
        version = entry["fields"].get("version")
        if not version or not entry["selectors"]:
            continue
        name = entry["selectors"][0][0]
        acc.add_release(
            name,
            version,
            resolved_url=entry["fields"].get("resolved"),
            integrity=entry["fields"].get("integrity"),
        )
        for dep, constraint in {**entry["deps"], **entry["optional"]}.items():
            dst_version = by_selector.get((dep, constraint))
            if dst_version is None:
                continue  # optional dep not installed
            acc.add_edge(name, version, dep, constraint, dst_version)

    _root_edges_from_manifest(manifest, lambda dep, rng: by_selector.get((dep, rng)), acc)
    # yarn.lock v1 records no dev flags anywhere; derive them.
    acc.apply_dev_reachability()


# --------------------------------------------------------------------------
# pnpm-lock.yaml v6/v9
# --------------------------------------------------------------------------


def _pnpm_split(key: str) -> tuple[str, str] | None:
    """``/pkg@1.0.0``, ``pkg@1.0.0``, ``@s/p@1.0.0(peer@2.0.0)`` -> (name, version)."""
    if key.startswith("/"):
        key = key[1:]
    paren = key.find("(")
    if paren != -1:
        key = key[:paren]
    at = key.rfind("@")
    if at <= 0:
        return None
    return key[:at], key[at + 1:]


def _pnpm_dep_target(dep_name: str, value: Any) -> tuple[str, str] | None:
    """Resolve a pnpm dependency value to ``(real name, exact version)``."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(("link:", "file:", "workspace:")):
        return None
    if value.startswith("npm:"):  # alias: npm:real-name@1.2.3
        return _pnpm_split(value[4:])
    if value.startswith("/"):  # absolute form used by some versions
        return _pnpm_split(value)
    paren = value.find("(")
    return dep_name, (value[:paren] if paren != -1 else value)


def _parse_pnpm(data: dict[str, Any], acc: _Accumulator) -> None:
    packages: dict[str, Any] = data.get("packages") or {}
    snapshots: dict[str, Any] | None = data.get("snapshots")
    importers: dict[str, Any] | None = data.get("importers")
    if importers is None:
        # v6 single-project form keeps the root sections at the top level.
        importers = {
            ".": {
                section: data.get(section) or {}
                for section in ("dependencies", "devDependencies", "optionalDependencies")
            }
        }

    # v6 stamps `dev: true/false` on packages; v9 dropped it, so dev-ness must
    # be derived by reachability instead.
    explicit_dev = any(isinstance(e, dict) and "dev" in e for e in packages.values())

    dev_releases: set[tuple[str, str]] = set()
    for key, entry in packages.items():
        split = _pnpm_split(str(key))
        if split is None or not isinstance(entry, dict):
            continue
        name, version = split
        resolution = entry.get("resolution") if isinstance(entry.get("resolution"), dict) else {}
        dev = bool(entry.get("dev"))
        acc.add_release(
            name,
            version,
            dev=dev,
            resolved_url=resolution.get("tarball"),
            integrity=resolution.get("integrity"),
        )
        if dev:
            dev_releases.add((name, version))

    # v9 keeps the dependency graph in `snapshots`; v6 keeps it on `packages`.
    for key, entry in (snapshots if snapshots is not None else packages).items():
        split = _pnpm_split(str(key))
        if split is None or not isinstance(entry, dict):
            continue
        src_name, src_version = split
        if snapshots is not None:
            acc.add_release(src_name, src_version)  # snapshot-only entries still exist
        src_dev = (src_name, src_version) in dev_releases
        for section in ("dependencies", "optionalDependencies"):
            for dep_name, value in (entry.get(section) or {}).items():
                target = _pnpm_dep_target(str(dep_name), value)
                if target is None:
                    continue
                dst_name, dst_version = target
                # pnpm snapshots record resolved versions, not ranges, so the
                # exact version doubles as the constraint here.
                acc.add_edge(src_name, src_version, dst_name, dst_version, dst_version, dev=src_dev)

    for importer_path, importer in importers.items():
        if not isinstance(importer, dict):
            continue
        is_root = importer_path in (".", "")
        for section, dev in (
            ("dependencies", False),
            ("optionalDependencies", False),
            ("devDependencies", True),
        ):
            for dep_name, spec in (importer.get(section) or {}).items():
                if isinstance(spec, dict):
                    specifier = str(spec.get("specifier") or "")
                    value = spec.get("version")
                else:  # pre-v6 importers keep bare versions here
                    specifier, value = "", spec
                target = _pnpm_dep_target(str(dep_name), value)
                if target is None:
                    continue
                dst_name, dst_version = target
                acc.add_edge(None, None, dst_name, specifier or dst_version, dst_version, dev=dev)
                if is_root:
                    acc.mark_direct(dst_name, dst_version)

    if not explicit_dev:
        acc.apply_dev_reachability()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def parse_lockfile(path: Path, repo_root: Path) -> ParsedLockfile:
    """Parse one lockfile into the IR. ``path`` is stored repo-relative."""
    path = Path(path)
    repo_root = Path(repo_root)
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    manifest = _read_json(path.parent / "package.json") or {}
    acc = _Accumulator()
    filename = path.name

    if filename == "package-lock.json":
        kind = "npm"
        data = _read_json(path)
        if data is None:
            raise ValueError(f"unreadable package-lock.json: {path}")
        if "packages" in data:  # lockfileVersion 2/3
            _parse_npm_new(data, acc)
        else:  # lockfileVersion 1
            _parse_npm_v1(data, manifest, acc)
        root_name = data.get("name") or manifest.get("name") or repo_root.name
    elif filename == "yarn.lock":
        kind = "yarn"
        _parse_yarn(path.read_text(encoding="utf-8"), manifest, acc)
        root_name = manifest.get("name") or repo_root.name
    elif filename in ("pnpm-lock.yaml", "pnpm-lock.yml"):
        kind = "pnpm"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"unreadable pnpm lockfile: {path}")
        _parse_pnpm(data, acc)
        root_name = manifest.get("name") or repo_root.name
    else:
        raise ValueError(f"unrecognised lockfile: {path.name}")

    releases, edges = acc.build()
    return ParsedLockfile(
        path=rel,
        kind=kind,
        root_name=str(root_name),
        releases=releases,
        edges=edges,
    )
