"""Real remediation branches - the fix for the fabricated-diff problem.

``open_pr`` clones the target repository into a scratch directory, creates a
``radix/pin-<package>-<safe_version>`` branch, merges an npm ``overrides``
entry into the manifest that declares the package, regenerates the lockfile
with ``npm install --package-lock-only --ignore-scripts`` (real registry
integrity hashes, no package code ever executes), and commits as
"Radix Sentinel <radix@localhost>". In dry-run mode (the default) it stops
there and returns the real diff; with ``dry_run=False`` plus a GitHub token
and an ``https://github.com/...`` target it pushes the branch and opens the
pull request via the REST API.

Safety invariants:
  * ``--ignore-scripts`` on every npm invocation - dependency code never runs.
  * All work happens in the scratch clone; a local-path ``repo_target`` is
    never written to.
  * The branch is always ``radix/...`` - never ``main``/``master``.
  * The GitHub token is used only inside subprocess argv / request headers and
    is scrubbed from every string that could surface (returned, raised or
    logged).

The returned dict is the frozen ``OpenPrResponse`` shape from
``frontend/src/lib/types.ts`` / ``docs/API_CONTRACT.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import requests

log = logging.getLogger("radix.pr_engine")

__all__ = ["open_pr", "merge_overrides", "PrEngineError"]


class PrEngineError(RuntimeError):
    """Raised for any failure the caller should surface verbatim (no token)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REMOTE_URL = re.compile(r"^(?:https?://|git://|ssh://|git@)")
_GITHUB_HTTPS = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_LOCKFILE_NAMES = ("package-lock.json", "npm-shrinkwrap.json")
_PROTECTED = {"main", "master"}
_GIT_TIMEOUT_S = 120
_NPM_TIMEOUT_S = 300
_AUTHOR = "Radix Sentinel <radix@localhost>"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _redact(text: str, secret: str | None) -> str:
    """Scrub ``secret`` from ``text``. Applied to every outward-facing string."""
    if not secret or not text:
        return text
    return text.replace(secret, "***")


def _strip_credentials(url: str) -> str:
    """Remove any ``user:pass@`` userinfo from a URL before echoing it back."""
    return re.sub(r"^([a-z+]+://)[^/@]+@", r"\1", url)


def _run(cmd: list[str], *, cwd: Path | None, timeout: int) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0: an unauthenticated clone of a private repo must
    # fail immediately with a clear error, not hang waiting for a username on
    # a terminal the server does not have.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _git(args: list[str], cwd: Path, *, secret: str | None = None) -> str:
    """Run git, raising ``PrEngineError`` (token-scrubbed) on failure."""
    proc = _run(["git", *args], cwd=cwd, timeout=_GIT_TIMEOUT_S)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        # Only ever name the subcommand: full argv could carry a tokened URL.
        raise PrEngineError(f"git {args[0]} failed: {_redact(detail, secret)}")
    return proc.stdout


def _slug(package_name: str) -> str:
    """Branch-safe fragment for a package name (handles @scope/name)."""
    cleaned = package_name.lstrip("@").replace("/", "-")
    return re.sub(r"[^0-9A-Za-z._-]+", "-", cleaned).strip("-.") or "package"


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------


def merge_overrides(manifest: Mapping[str, Any], package: str, version: str) -> dict[str, Any]:
    """Return a copy of ``manifest`` with ``overrides[package] = version``.

    Existing overrides entries (including nested object forms npm allows) are
    preserved untouched; only the one key is set/replaced.
    """
    merged = dict(manifest)
    overrides = dict(manifest.get("overrides") or {})
    overrides[package] = version
    merged["overrides"] = overrides
    return merged


def _declares_package(manifest_dir: Path, manifest: Mapping[str, Any], package: str) -> bool:
    """True if the manifest names the package directly or its lockfile resolves it."""
    for field in _DEP_FIELDS:
        deps = manifest.get(field)
        if isinstance(deps, dict) and package in deps:
            return True
    for name in _LOCKFILE_NAMES:
        lock = manifest_dir / name
        if not lock.is_file():
            continue
        try:
            text = lock.read_text(encoding="utf-8")
        except OSError:
            continue
        if f'"node_modules/{package}"' in text or f'"{package}":' in text:
            return True
    return False


def _find_manifest_dir(root: Path, package: str) -> Path:
    """package.json at the root or one level down that declares ``package``."""
    candidates = [root]
    candidates += sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and child.name not in {".git", "node_modules"}
    )
    scanned: list[str] = []
    for candidate in candidates:
        manifest_path = candidate / "package.json"
        if not manifest_path.is_file():
            continue
        scanned.append(str(manifest_path.relative_to(root)))
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and _declares_package(candidate, manifest, package):
            return candidate
    raise PrEngineError(
        f"no package.json declaring {package!r} found at the repo root or one level "
        f"down (scanned: {', '.join(scanned) or 'none'})"
    )


def _apply_overrides(manifest_dir: Path, package: str, version: str) -> dict[str, str]:
    """Merge the pin into package.json on disk; return the full overrides map.

    When the package is also a *direct* dependency its spec is pinned to the
    same version - npm refuses to resolve (``EOVERRIDE``) when an override
    conflicts with a direct dependency, and pinning both is the remediation a
    human reviewer would expect to see anyway.
    """
    manifest_path = manifest_dir / "package.json"
    text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(text)
    merged = merge_overrides(manifest, package, version)
    for field in _DEP_FIELDS:
        deps = merged.get(field)
        if isinstance(deps, dict) and package in deps and deps[package] != version:
            merged[field] = {**deps, package: version}
    indent_match = re.search(r'^([ \t]+)"', text, re.M)
    indent: str | int = indent_match.group(1) if indent_match else 2
    manifest_path.write_text(json.dumps(merged, indent=indent) + "\n", encoding="utf-8")
    return {k: v for k, v in merged["overrides"].items() if isinstance(v, str)}


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def _clone(repo_target: str, dest: Path) -> None:
    """Shallow-clone a remote URL, or locally clone a path, into ``dest``."""
    if _REMOTE_URL.match(repo_target):
        try:
            _git(["clone", "--depth", "1", repo_target, str(dest)], dest.parent)
        except PrEngineError:
            # Anonymous https fails on private GitHub repos. When the host has
            # an SSH identity (a dev machine typically does), the ssh form of
            # the same repo is worth one retry before giving up.
            match = re.match(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", repo_target)
            if not match:
                raise
            _git(["clone", "--depth", "1", f"git@github.com:{match.group(1)}.git", str(dest)], dest.parent)
        if not (dest / ".git").exists():
            raise PrEngineError("clone produced no git repository")
        return
    else:
        src = Path(repo_target).expanduser().resolve()
        if not src.is_dir():
            raise PrEngineError(f"repo_target {repo_target!r} is not a directory or git URL")
        if not (src / ".git").exists():
            raise PrEngineError(f"local repo_target {repo_target!r} is not a git repository")
        # --no-hardlinks: the scratch clone shares nothing with the source
        # object store, so nothing we do can reach back into it.
        cmd = ["clone", "--no-hardlinks", str(src), str(dest)]
        cwd = dest.parent
    _git(cmd, cwd)
    if not (dest / ".git").exists():
        raise PrEngineError("clone produced no git repository")


def _regenerate_lockfile(manifest_dir: Path) -> tuple[bool, str]:
    """npm lockfile-only resolve. --ignore-scripts is non-negotiable.

    Degrades gracefully (False + reason) when npm or the registry is
    unavailable - the overrides edit alone is still a valid remediation.
    """
    cmd = [
        "npm",
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    try:
        proc = _run(cmd, cwd=manifest_dir, timeout=_NPM_TIMEOUT_S)
    except FileNotFoundError:
        return False, "npm is not installed on this host"
    except subprocess.TimeoutExpired:
        return False, f"npm timed out after {_NPM_TIMEOUT_S}s"
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])
        return False, f"npm --package-lock-only failed ({tail})"
    return True, "npm regenerated the lockfile against the registry"


def _commit(
    clone_dir: Path,
    *,
    package_name: str,
    safe_version: str,
    bad_version: str | None,
    service_name: str,
) -> str:
    """Stage and commit as Radix Sentinel; returns the commit title."""
    title = f"fix(security): pin {package_name} to {safe_version}"
    bad = bad_version or "the compromised release"
    body = (
        f"{package_name} {bad} was flagged as compromised by the Radix "
        f"supply-chain sentinel while analysing service {service_name!r}.\n\n"
        f'This commit adds an npm "overrides" pin forcing every transitive\n'
        f"resolution of {package_name} to {safe_version} and regenerates the\n"
        f"lockfile with --ignore-scripts (no dependency code was executed).\n\n"
        f"Generated-by: Radix Sentinel"
    )
    _git(["add", "-A"], clone_dir)
    _git(
        [
            "-c",
            "user.name=Radix Sentinel",
            "-c",
            "user.email=radix@localhost",
            "commit",
            f"--author={_AUTHOR}",
            "-m",
            title,
            "-m",
            body,
        ],
        clone_dir,
    )
    return title


def _pr_body(
    *,
    package_name: str,
    safe_version: str,
    bad_version: str | None,
    service_name: str,
    regenerated: bool,
) -> str:
    lock_line = (
        "The lockfile was regenerated with `npm install --package-lock-only "
        "--ignore-scripts`, so every integrity hash comes from the registry."
        if regenerated
        else "The lockfile could not be regenerated in the sandbox; run "
        "`npm install --package-lock-only --ignore-scripts` locally to refresh it."
    )
    return "\n".join(
        [
            "## Radix security remediation",
            "",
            f"- **Package**: `{package_name}`",
            f"- **Compromised release**: `{bad_version or 'unknown'}`",
            f"- **Pinned safe release**: `{safe_version}`",
            f"- **Service**: `{service_name}`",
            "",
            f'This PR pins `{package_name}` to `{safe_version}` via npm `"overrides"`, '
            "which forces the safe version onto every transitive resolution as well "
            "as direct ones.",
            "",
            lock_line,
            "",
            "_Opened automatically by Radix Sentinel. No package code was executed "
            "while preparing this branch._",
        ]
    )


def _push_and_open_pr(
    clone_dir: Path,
    *,
    repo_target: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    token: str,
) -> str:
    """Push the branch with a tokened remote URL and open the PR. Returns pr_url."""
    match = _GITHUB_HTTPS.match(repo_target)
    if not match:
        raise PrEngineError(
            "push requires an https://github.com/<owner>/<repo> repo_target; "
            f"got {_strip_credentials(repo_target)!r}"
        )
    owner, repo = match.group(1), match.group(2)
    if branch in _PROTECTED or branch == base:
        raise PrEngineError(f"refusing to push to branch {branch!r}")

    # The token exists only inside argv here; _git names just the subcommand
    # on failure and scrubs the secret from captured output.
    push_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    _git(["push", push_url, f"{branch}:refs/heads/{branch}"], clone_dir, secret=token)

    response = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "head": branch, "base": base, "body": body},
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise PrEngineError(
            f"GitHub PR creation failed ({response.status_code}): "
            f"{_redact(response.text[:500], token)}"
        )
    pr_url = response.json().get("html_url")
    if not isinstance(pr_url, str):
        raise PrEngineError("GitHub PR creation returned no html_url")
    return pr_url


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def open_pr(
    *,
    repo_target: str,
    package_name: str,
    safe_version: str,
    bad_version: str | None,
    service_name: str,
    dry_run: bool = True,
    github_token: str | None = None,
    workdir: Path | None = None,
) -> dict:
    """Create a real remediation branch; return the frozen OpenPrResponse dict.

    ``dry_run=True`` (default) does everything except push: clone, branch,
    overrides, lockfile regeneration, commit - then reports the real diff and
    discards the scratch clone. ``dry_run=False`` additionally requires
    ``github_token`` and an ``https://github.com/...`` target.
    """
    if not dry_run:
        if not github_token:
            raise PrEngineError(
                "dry_run=False requires a GitHub token (set GITHUB_TOKEN on the "
                "backend); without it only dry-run mode is available"
            )
        if not _GITHUB_HTTPS.match(repo_target):
            raise PrEngineError(
                "dry_run=False requires an https://github.com/<owner>/<repo> "
                f"repo_target; got {_strip_credentials(repo_target)!r}"
            )

    branch = f"radix/pin-{_slug(package_name)}-{safe_version}"
    if workdir is not None:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="radix-pr-", dir=str(workdir) if workdir else None))
    clone_dir = scratch / "clone"

    try:
        _clone(repo_target, clone_dir)
        base = _git(["rev-parse", "--abbrev-ref", "HEAD"], clone_dir).strip()
        _git(["checkout", "-b", branch], clone_dir)

        manifest_dir = _find_manifest_dir(clone_dir, package_name)
        overrides = _apply_overrides(manifest_dir, package_name, safe_version)
        regenerated, regen_note = _regenerate_lockfile(manifest_dir)

        title = _commit(
            clone_dir,
            package_name=package_name,
            safe_version=safe_version,
            bad_version=bad_version,
            service_name=service_name,
        )
        diff = _git(["show", "HEAD", "--patch", "--no-color"], clone_dir)

        repo_display = _redact(_strip_credentials(repo_target), github_token)
        if dry_run:
            message = (
                f"Dry run: branch {branch} was created from {base} in a scratch "
                f"clone, pinning {package_name} to {safe_version}"
                + (f" (bad release {bad_version})" if bad_version else "")
                + f" for service {service_name}. {regen_note}. Nothing was pushed "
                "and the scratch clone was discarded; set GITHUB_TOKEN and "
                "dry_run=false to push this branch and open the pull request."
            )
            log.info("open_pr dry-run complete: %s on %s", branch, repo_display)
            return {
                "mode": "dry-run",
                "repo": repo_display,
                "branch": branch,
                "base": base,
                "diff": diff,
                "overrides": overrides,
                "regenerated": regenerated,
                "message": _redact(message, github_token),
            }

        assert github_token is not None  # validated above
        body = _pr_body(
            package_name=package_name,
            safe_version=safe_version,
            bad_version=bad_version,
            service_name=service_name,
            regenerated=regenerated,
        )
        pr_url = _push_and_open_pr(
            clone_dir,
            repo_target=repo_target,
            branch=branch,
            base=base,
            title=title,
            body=body,
            token=github_token,
        )
        message = (
            f"Pushed {branch} and opened a pull request against {base}. "
            f"{regen_note}."
        )
        log.info("open_pr pushed: %s -> %s", branch, pr_url)
        return {
            "mode": "pushed",
            "repo": repo_display,
            "branch": branch,
            "base": base,
            "diff": diff,
            "overrides": overrides,
            "pr_url": pr_url,
            "regenerated": regenerated,
            "message": _redact(message, github_token),
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
