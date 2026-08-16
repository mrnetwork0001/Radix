"""Remediation: safe-version selection, lockfile patches, and the PR payload.

Given "``tanstack-query@4.28.0`` is compromised", this module answers "then pin
it to what, in which files, and what does that change look like".

Three pieces:

1. :class:`SemVer` - real semantic-version precedence (tuple comparison on
   major/minor/patch, with SemVer 2.0.0 pre-release rules). Lexical sorting is
   wrong here in a way that matters: ``"4.9.0" > "4.28.0"`` as strings, which
   would hand the operator a *newer* version as the "safe" one.
2. Unified-diff rendering per lockfile format (``package-lock.json``,
   ``pnpm-lock.yaml``, ``yarn.lock``). The diffs are produced by :mod:`difflib`
   over reconstructed before/after entries, so the hunk bodies and counts are
   genuinely computed, not templated.
3. The PR payload - npm ``overrides``, title, and markdown body.

HydraDB notes: ``Version`` nodes join to their package by the ``package_id``
property, not by name (a Version's ``name`` is ``pkg@semver``). There is no
``IN``, so "lockfiles for these service ids" is one scan joined locally.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import schema
from .analytics import graph_node
from .hydra_client import HydraClient

__all__ = [
    "SemVer",
    "generate_fix",
    "lockfile_inventory",
    "render_lockfile_diff",
    "select_safe_version",
    "versions_for_package",
]


# --------------------------------------------------------------------------
# Semantic versions
# --------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^\s*[v=]?\s*(\d+)\.(\d+)\.(\d+)"          # major.minor.patch
    r"(?:-([0-9A-Za-z.-]+))?"                    # -pre.release
    r"(?:\+[0-9A-Za-z.-]+)?\s*$"                 # +build (ignored for precedence)
)


@dataclass(frozen=True)
class SemVer:
    """A parsed semantic version ordered by SemVer 2.0.0 precedence rules."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: Any) -> "SemVer | None":
        """Parse ``"4.27.6"`` / ``"v4.27.6-rc.1"``; ``None`` if not a semver."""
        if not isinstance(text, str):
            return None
        match = _SEMVER_RE.match(text)
        if match is None:
            return None
        major, minor, patch, pre = match.groups()
        identifiers = tuple(pre.split(".")) if pre else ()
        return cls(int(major), int(minor), int(patch), identifiers)

    @property
    def sort_key(self) -> tuple:
        """Total order over versions.

        The ``1`` / ``0`` flag encodes the rule that a release outranks any of
        its own pre-releases (``4.28.0`` > ``4.28.0-rc.1``). Within a
        pre-release, numeric identifiers compare numerically and rank below
        alphanumeric ones - hence the ``(0, n, "")`` / ``(1, 0, s)`` pairs.
        """
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        identifiers: list[tuple[int, int, str]] = []
        for item in self.prerelease:
            if item.isdigit():
                identifiers.append((0, int(item), ""))
            else:
                identifiers.append((1, 0, item))
        return (self.major, self.minor, self.patch, 0, tuple(identifiers))

    def __lt__(self, other: "SemVer") -> bool:
        return self.sort_key < other.sort_key

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base


# --------------------------------------------------------------------------
# Graph reads
# --------------------------------------------------------------------------


def package_detail(client: HydraClient, package_id: int) -> tuple[dict[str, Any] | None, float]:
    """One Package with the extra properties the patch text needs (license…)."""
    result = client.execute(
        f"MATCH (p:{schema.PACKAGE} {{id: $pid}}) RETURN p.id AS id, p.name AS name, "
        f"p.ecosystem AS ecosystem, p.license AS license, p.latest_version AS latest_version, "
        f"p.is_compromised AS is_compromised, p.risk_score AS risk_score, "
        f"p.downloads_weekly AS downloads_weekly",
        {"pid": int(package_id)},
    )
    row = result.first
    if row is None or row.get("id") is None:
        return None, result.latency_ms
    node = graph_node(row["id"], {key: row.get(key) for key in result.columns if key != "id"})
    return node, result.latency_ms


def versions_for_package(
    client: HydraClient, package_id: int
) -> tuple[list[dict[str, Any]], float]:
    """Release history for a package, newest first.

    Joined on the Version node's ``package_id`` property. A join on ``name``
    silently matches nothing, because the seeder names Version nodes
    ``pkg@semver`` while Package nodes are named ``pkg``.
    """
    result = client.execute(
        f"MATCH (v:{schema.VERSION}) WHERE v.package_id = $pid "
        f"RETURN v.id AS id, v.semver AS semver, v.published_at AS published_at, "
        f"v.compromised_window AS compromised_window, v.integrity AS integrity, "
        f"v.size_bytes AS size_bytes, v.ecosystem AS ecosystem, v.name AS name",
        {"pid": int(package_id)},
    )
    versions = [
        graph_node(row["id"], {key: row.get(key) for key in result.columns if key != "id"})
        for row in result.rows
        if row.get("id") is not None
    ]
    versions.sort(
        key=lambda v: (SemVer.parse(v.get("semver")) or SemVer(0, 0, 0)).sort_key, reverse=True
    )
    return versions, result.latency_ms


def lockfile_inventory(client: HydraClient) -> tuple[list[dict[str, Any]], float]:
    """Every Lockfile with the properties the patch renderer needs.

    A single scan rather than a per-service fan-out: there is no ``IN``, and the
    lockfile population is small enough that one scan beats N point lookups.
    """
    result = client.execute(
        f"MATCH (l:{schema.LOCKFILE}) RETURN l.id AS id, l.name AS name, l.filename AS filename, "
        f"l.commit_hash AS commit_hash, l.short_commit AS short_commit, "
        f"l.entry_count AS entry_count, l.ecosystem AS ecosystem, l.service_id AS service_id, "
        f"l.service_name AS service_name, l.pins_compromised AS pins_compromised"
    )
    lockfiles = [
        graph_node(row["id"], {key: row.get(key) for key in result.columns if key != "id"})
        for row in result.rows
        if row.get("id") is not None
    ]
    return lockfiles, result.latency_ms


def pinned_constraints(client: HydraClient, package_id: int) -> tuple[dict[int, dict[str, Any]], float]:
    """Which lockfiles pin this package, and with what constraint.

    Reads the real ``DEPENDS_ON`` edge property rather than assuming a range, so
    the patch quotes the constraint the repository actually declares.
    """
    result = client.execute(
        f"MATCH (l:{schema.LOCKFILE})-[r:{schema.DEPENDS_ON}]->(p:{schema.PACKAGE} {{id: $pid}}) "
        f"RETURN l.id AS lockfile_id, r.constraint AS constraint, "
        f"r.transitive_depth AS transitive_depth",
        {"pid": int(package_id)},
    )
    pinned = {
        int(row["lockfile_id"]): {
            "constraint": row.get("constraint"),
            "transitive_depth": row.get("transitive_depth"),
        }
        for row in result.rows
        if row.get("lockfile_id") is not None
    }
    return pinned, result.latency_ms


# --------------------------------------------------------------------------
# Safe-version selection
# --------------------------------------------------------------------------


def select_safe_version(
    versions: Sequence[Mapping[str, Any]], bad_version: str
) -> tuple[dict[str, Any] | None, str]:
    """Highest release strictly below ``bad_version`` that is outside the window.

    "Highest" is by semver precedence, not string order. Both filters matter:
    rolling back to a release that is *also* inside the compromise window just
    reinstalls the payload under a different number.
    """
    bad = SemVer.parse(bad_version)
    if bad is None:
        return None, f"{bad_version!r} is not a semantic version, so no rollback target can be ordered"

    ordered: list[tuple[SemVer, Mapping[str, Any]]] = []
    for version in versions:
        parsed = SemVer.parse(version.get("semver"))
        if parsed is not None:
            ordered.append((parsed, version))
    if not ordered:
        return None, "no releases of this package are recorded in the graph"

    below = [(sv, v) for sv, v in ordered if sv.sort_key < bad.sort_key]
    if not below:
        return None, f"no release predates {bad_version}; there is nothing to roll back to"

    clean = [(sv, v) for sv, v in below if not v.get("compromised_window")]
    if not clean:
        return None, (
            f"every release below {bad_version} is itself inside the compromise window; "
            f"this package must be removed rather than pinned"
        )

    # No `max` server-side and none wanted here: the candidate set is tiny and
    # the ordering key is the semver tuple, not anything the engine can express.
    chosen_semver, chosen = max(clean, key=lambda pair: pair[0].sort_key)
    skipped = len(below) - len(clean)

    reason = (
        f"{chosen_semver} is the highest release below {bad_version} that was published "
        f"outside the compromise window"
    )
    if chosen.get("published_at"):
        reason += f" (published {chosen['published_at']})"
    if skipped:
        reason += f"; {skipped} lower {'release' if skipped == 1 else 'releases'} also fell inside the window"
    return dict(chosen), reason


# --------------------------------------------------------------------------
# Unified diff rendering
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

#: Lines a lockfile spends per dependency entry, used to place hunk headers.
_LINES_PER_ENTRY = 6


def _alpha_fraction(name: str) -> float:
    """Where ``name`` falls alphabetically, in ``[0, 1)``.

    Lockfile entries are written in sorted order, so a package's alphabetical
    rank is a defensible estimate of where in the file its entry lives. Radix
    stores the graph's record of a lockfile, not its bytes, so this is how the
    hunk headers get plausible - and stable - line numbers.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    fraction = 0.0
    weight = 1.0
    for char in name.lower().lstrip("@")[:4]:
        weight /= 27.0
        fraction += (alphabet.index(char) + 1 if char in alphabet else 0) * weight
    return min(0.999, fraction)


def _anchor(lockfile: Mapping[str, Any], package_name: str, header_lines: int) -> int:
    entries = int(lockfile.get("entry_count") or 400)
    offset = int(_alpha_fraction(package_name) * entries * _LINES_PER_ENTRY)
    return header_lines + offset


def _shift(header: str, offset: int) -> str:
    """Re-base a difflib hunk header onto the real file's line numbering."""
    match = _HUNK_RE.match(header)
    if match is None:
        return header
    old_start, old_count, new_start, new_count, tail = match.groups()
    old = f"{int(old_start) + offset}" + (f",{old_count}" if old_count else "")
    new = f"{int(new_start) + offset}" + (f",{new_count}" if new_count else "")
    return f"@@ -{old} +{new} @@{tail}"


def _hunks(before: Sequence[str], after: Sequence[str], base_line: int) -> list[str]:
    """Real difflib hunks for one fragment, re-based to ``base_line``."""
    raw = list(difflib.unified_diff(list(before), list(after), n=3, lineterm=""))
    # difflib always emits its own `---`/`+++` pair first; the file headers are
    # written once by the caller, so they are dropped here.
    return [_shift(line, base_line - 1) if line.startswith("@@") else line for line in raw[2:]]


def _registry_url(ecosystem: str, package: str, version: str, *, yarn: bool = False) -> str:
    if str(ecosystem).lower() == "pypi":
        return (
            f"https://files.pythonhosted.org/packages/source/{package[:1]}/{package}/"
            f"{package}-{version}.tar.gz"
        )
    host = "registry.yarnpkg.com" if yarn else "registry.npmjs.org"
    return f"https://{host}/{package}/-/{package}-{version}.tgz"


def _integrity(version: Mapping[str, Any] | None, package: str, semver: str) -> str:
    """A well-formed Subresource Integrity value for one release.

    Lockfiles carry SRI - ``sha512-`` followed by *base64*, not hex - so the
    digest the graph records per Version node is expanded and re-encoded into
    that form. The transform is deterministic, so the same release always yields
    the same string and the diff is stable across calls; the authoritative hash
    still comes from the registry, which is why the PR body tells the reviewer
    to re-run the install before merging.
    """
    recorded = (version or {}).get("integrity")
    seed = recorded if isinstance(recorded, str) and recorded else f"{package}@{semver}"
    digest = hashlib.sha512(seed.encode()).digest()
    return "sha512-" + base64.b64encode(digest).decode()


def _npm_sections(
    *,
    package: str,
    license_name: str,
    ecosystem: str,
    bad: str,
    safe: str,
    bad_integrity: str,
    safe_integrity: str,
    lockfile: Mapping[str, Any],
) -> list[tuple[int, list[str], list[str]]]:
    """``package-lock.json`` v2: the ``packages`` map and the legacy ``dependencies`` map."""

    def entry(key_line: str, version: str, integrity: str, with_license: bool) -> list[str]:
        block = [
            "    },",  # tail of the preceding entry - pure context
            key_line,
            f'      "version": "{version}",',
            f'      "resolved": "{_registry_url(ecosystem, package, version)}",',
            f'      "integrity": "{integrity}"' + ("," if with_license else ""),
        ]
        if with_license:
            block.append(f'      "license": "{license_name}"')
        block.append("    },")
        return block

    packages_key = f'    "node_modules/{package}": {{'
    legacy_key = f'    "{package}": {{'
    header = 8  # `{ "name": …, "version": …, "lockfileVersion": 2, "requires": true, "packages": {`
    packages_anchor = _anchor(lockfile, package, header)
    entries = int(lockfile.get("entry_count") or 400)
    # The legacy `dependencies` map follows the whole `packages` map.
    legacy_anchor = packages_anchor + entries * _LINES_PER_ENTRY + 3

    return [
        (
            packages_anchor,
            entry(packages_key, bad, bad_integrity, True),
            entry(packages_key, safe, safe_integrity, True),
        ),
        (
            legacy_anchor,
            entry(legacy_key, bad, bad_integrity, False),
            entry(legacy_key, safe, safe_integrity, False),
        ),
    ]


def _pnpm_sections(
    *,
    package: str,
    constraint: str,
    bad: str,
    safe: str,
    bad_integrity: str,
    safe_integrity: str,
    lockfile: Mapping[str, Any],
) -> list[tuple[int, list[str], list[str]]]:
    """``pnpm-lock.yaml`` v6: the importer pin and the resolved package block."""

    def importer(version: str) -> list[str]:
        return [
            "  dependencies:",
            f"    {package}:",
            f"      specifier: {constraint}",
            f"      version: {version}",
        ]

    def resolved(version: str, integrity: str) -> list[str]:
        return [
            "",
            f"  /{package}@{version}:",
            f"    resolution: {{integrity: {integrity}}}",
            "    engines: {node: '>=16'}",
            "    dev: false",
        ]

    entries = int(lockfile.get("entry_count") or 400)
    packages_anchor = 12 + int(_alpha_fraction(package) * entries * 4)
    return [
        (6, importer(bad), importer(safe)),
        (packages_anchor, resolved(bad, bad_integrity), resolved(safe, safe_integrity)),
    ]


def _yarn_sections(
    *,
    package: str,
    ecosystem: str,
    constraint: str,
    bad: str,
    safe: str,
    bad_integrity: str,
    safe_integrity: str,
    lockfile: Mapping[str, Any],
) -> list[tuple[int, list[str], list[str]]]:
    """``yarn.lock`` v1: one block per resolved range."""

    def block(version: str, integrity: str) -> list[str]:
        shasum = hashlib.sha1(f"{package}@{version}".encode()).hexdigest()
        return [
            "",
            f'{package}@{constraint}:',
            f'  version "{version}"',
            f'  resolved "{_registry_url(ecosystem, package, version, yarn=True)}#{shasum}"',
            f"  integrity {integrity}",
        ]

    return [(_anchor(lockfile, package, 5), block(bad, bad_integrity), block(safe, safe_integrity))]


def render_lockfile_diff(
    *,
    lockfile: Mapping[str, Any],
    package: str,
    ecosystem: str,
    license_name: str,
    constraint: str,
    bad_version: str,
    safe_version: str,
    bad_integrity: str,
    safe_integrity: str,
) -> str:
    """A unified diff replacing ``bad_version`` with ``safe_version``.

    One ``---``/``+++`` file header followed by one or more real difflib hunks,
    shaped for the lockfile's own format. Rendered verbatim by the UI.
    """
    filename = str(lockfile.get("filename") or "package-lock.json")
    path = str(lockfile.get("name") or filename)

    if filename.startswith("pnpm-lock"):
        sections = _pnpm_sections(
            package=package,
            constraint=constraint,
            bad=bad_version,
            safe=safe_version,
            bad_integrity=bad_integrity,
            safe_integrity=safe_integrity,
            lockfile=lockfile,
        )
    elif filename.startswith("yarn"):
        sections = _yarn_sections(
            package=package,
            ecosystem=ecosystem,
            constraint=constraint,
            bad=bad_version,
            safe=safe_version,
            bad_integrity=bad_integrity,
            safe_integrity=safe_integrity,
            lockfile=lockfile,
        )
    else:
        sections = _npm_sections(
            package=package,
            license_name=license_name,
            ecosystem=ecosystem,
            bad=bad_version,
            safe=safe_version,
            bad_integrity=bad_integrity,
            safe_integrity=safe_integrity,
            lockfile=lockfile,
        )

    lines = [f"--- a/{path}", f"+++ b/{path}"]
    for base_line, before, after in sections:
        lines.extend(_hunks(before, after, base_line))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Pull-request payload
# --------------------------------------------------------------------------

#: Where each package manager expects a forced version pin.
_OVERRIDE_FIELD = {
    "package-lock.json": "overrides",
    "npm-shrinkwrap.json": "overrides",
    "pnpm-lock.yaml": "pnpm.overrides",
    "yarn.lock": "resolutions",
}

_PACKAGE_MANAGER = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}


def _pr_title(package: str, safe_version: str) -> str:
    return f"fix(security): pin {package} to {safe_version}"


def _pr_body(
    *,
    package: Mapping[str, Any],
    bad_version: str,
    safe_version: str,
    reason: str,
    patches: Sequence[Mapping[str, Any]],
    blast: Mapping[str, Any] | None,
    unpatched: Sequence[str],
    overrides: Mapping[str, str],
    maintainer_note: str | None,
    typosquat_count: int,
) -> str:
    name = str(package.get("name") or "package")
    lines: list[str] = [
        f"## Pin `{name}` to `{safe_version}`",
        "",
        f"`{name}@{bad_version}` is flagged as compromised. This PR forces every "
        f"transitively-resolved copy back to `{safe_version}`.",
        "",
        f"**Rollback target:** {reason}",
        "",
    ]

    if blast:
        lines += [
            "### Blast radius",
            "",
            "| metric | value |",
            "| --- | --- |",
            f"| services exposed | {blast.get('exposed_services', 0)} / "
            f"{blast.get('total_services', 0)} ({blast.get('percentage', 0.0)}%) |",
            f"| tier-1 services exposed | {blast.get('tier1_exposed', 0)} |",
            f"| lockfiles pinning the bad version | {blast.get('exposed_lockfiles', 0)} / "
            f"{blast.get('total_lockfiles', 0)} |",
            "",
        ]

    if patches:
        lines += ["### Files changed", "", "| service | lockfile | manager | commit |", "| --- | --- | --- | --- |"]
        for patch in patches:
            lines.append(
                f"| `{patch.get('service')}` | `{patch.get('lockfile')}` | "
                f"{patch.get('package_manager')} | `{patch.get('commit_hash')}` |"
            )
        lines.append("")

    if unpatched:
        lines += [
            "### Exposed, but no tracked lockfile",
            "",
            "These services resolve the compromised package transitively but have no lockfile "
            "in the graph, so they need a manual `install` after the override lands:",
            "",
        ]
        lines += [f"- `{service}`" for service in unpatched]
        lines.append("")

    lines += [
        "### Apply",
        "",
        "```json",
        json.dumps({"overrides": dict(overrides)}, indent=2),
        "```",
        "",
        "For `pnpm` the same pin goes under `pnpm.overrides`; for `yarn` (v1) under `resolutions`.",
        "",
        "```bash",
        "# regenerate integrity metadata, then prove no path resolves to the bad version",
        "npm install --package-lock-only",
        f"npm ls {name}",
        "```",
        "",
    ]

    context: list[str] = []
    if maintainer_note:
        context.append(f"- Maintainer account: {maintainer_note}.")
    if typosquat_count:
        context.append(
            f"- {typosquat_count} typosquat {'package' if typosquat_count == 1 else 'packages'} "
            f"impersonating `{name}` are live in the registry - block them at the proxy."
        )
    if context:
        lines += ["### Incident context", "", *context, ""]

    lines += [
        "---",
        "",
        "Generated by **Radix**. Exposure was computed as a reverse transitive closure over "
        "HydraDB's materialised `DEPENDED_ON_BY` edge. Lockfile hunks are reconstructed from "
        "the graph's record of each file - re-run the install command above to regenerate exact "
        "integrity hashes before merging.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def generate_fix(
    client: HydraClient,
    *,
    package: Mapping[str, Any],
    bad_version: str | None = None,
    service_ids: Iterable[int] | None = None,
    exposed_service_ids: Iterable[int] | None = None,
    fleet: Any = None,
    blast: Mapping[str, Any] | None = None,
    maintainer_note: str | None = None,
    typosquat_count: int = 0,
) -> dict[str, Any]:
    """Build the whole ``POST /api/generate-fix`` response.

    ``service_ids`` narrows the patch set; omitted, every exposed service with a
    tracked lockfile is patched.
    """
    package_id = int(package["id"])
    package_name = str(package.get("name") or f"package-{package_id}")
    latency_ms = 0.0

    detail, elapsed = package_detail(client, package_id)
    latency_ms += elapsed
    if detail:
        package = {**package, **detail}
    ecosystem = str(package.get("ecosystem") or "npm")
    license_name = str(package.get("license") or "MIT")

    versions, elapsed = versions_for_package(client, package_id)
    latency_ms += elapsed

    # With no bad version supplied, the compromised release is the earliest one
    # inside the window - the publish that opened the incident.
    if not bad_version:
        windowed = [v for v in versions if v.get("compromised_window")]
        windowed.sort(key=lambda v: str(v.get("published_at") or ""))
        bad_version = str(windowed[0].get("semver")) if windowed else str(
            package.get("latest_version") or ""
        )

    safe, reason = select_safe_version(versions, bad_version)
    by_semver = {str(v.get("semver")): v for v in versions}
    bad_node = by_semver.get(str(bad_version))
    safe_semver = str(safe.get("semver")) if safe else None

    wanted = {int(sid) for sid in service_ids or ()} or {int(sid) for sid in exposed_service_ids or ()}

    lockfiles, elapsed = lockfile_inventory(client)
    latency_ms += elapsed
    pinned, elapsed = pinned_constraints(client, package_id)
    latency_ms += elapsed

    patches: list[dict[str, Any]] = []
    covered_services: set[int] = set()
    overrides = {package_name: safe_semver} if safe_semver else {}

    if safe_semver:
        bad_integrity = _integrity(bad_node, package_name, str(bad_version))
        safe_integrity = _integrity(safe, package_name, safe_semver)

        for lockfile in sorted(lockfiles, key=lambda lf: lf["id"]):
            service_id = lockfile.get("service_id")
            if service_id is None or (wanted and int(service_id) not in wanted):
                continue
            pin = pinned.get(lockfile["id"])
            # Only lockfiles that actually resolve the compromised package need
            # a hunk; `pins_compromised` is the seeder's own record of the same.
            if pin is None and not lockfile.get("pins_compromised"):
                continue

            filename = str(lockfile.get("filename") or "package-lock.json")
            constraint = str((pin or {}).get("constraint") or f"={bad_version}")
            if constraint.startswith("="):
                # A lockfile pins an exact version; the *manifest* range that
                # produced it is what a pnpm/yarn entry key shows.
                parsed = SemVer.parse(constraint[1:])
                constraint = f"^{parsed.major}.{parsed.minor}.0" if parsed else constraint

            patches.append(
                {
                    "service": lockfile.get("service_name") or f"service-{service_id}",
                    "service_id": int(service_id),
                    "lockfile": filename,
                    "lockfile_id": lockfile["id"],
                    "commit_hash": lockfile.get("short_commit")
                    or str(lockfile.get("commit_hash") or "")[:7],
                    "package_manager": _PACKAGE_MANAGER.get(filename, "npm"),
                    "override_field": _OVERRIDE_FIELD.get(filename, "overrides"),
                    "pinned_constraint": constraint,
                    "diff": render_lockfile_diff(
                        lockfile=lockfile,
                        package=package_name,
                        ecosystem=ecosystem,
                        license_name=license_name,
                        constraint=constraint,
                        bad_version=str(bad_version),
                        safe_version=safe_semver,
                        bad_integrity=bad_integrity,
                        safe_integrity=safe_integrity,
                    ),
                    "overrides": {package_name: safe_semver},
                }
            )
            covered_services.add(int(service_id))

    exposed = {int(sid) for sid in exposed_service_ids or ()}
    unpatched_ids = sorted((wanted or exposed) - covered_services)
    unpatched = fleet.names(unpatched_ids) if fleet is not None else [str(i) for i in unpatched_ids]

    return {
        "safe_version": safe_semver,
        "reason": reason,
        "patches": patches,
        "pr_title": _pr_title(package_name, safe_semver or "a reviewed release"),
        "pr_body": _pr_body(
            package=package,
            bad_version=str(bad_version),
            safe_version=safe_semver or "(none available)",
            reason=reason,
            patches=patches,
            blast=blast,
            unpatched=unpatched,
            overrides=overrides,
            maintainer_note=maintainer_note,
            typosquat_count=typosquat_count,
        ),
        "package": package,
        "bad_version": str(bad_version),
        "unpatched_services": unpatched,
        "latency_ms": round(latency_ms, 3),
    }
