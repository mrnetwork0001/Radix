"""Repository acquisition and scanning.

``scan_source`` accepts a local directory or a git URL. Local paths are scanned
in place and never modified; URLs are shallow-cloned into a working directory
and the clone is removed once parsed. Only :mod:`app.ingest.lockfiles` looks at
file contents - this module just gets a tree on disk and records provenance.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .lockfiles import discover_lockfiles, parse_lockfile
from .model import ParsedLockfile, RepoScan

__all__ = ["scan_source"]

logger = logging.getLogger(__name__)

_GIT_URL_RE = re.compile(r"^(https?://|git://|ssh://|git@)")
_CLONE_TIMEOUT_S = 300


def _is_git_url(target: str) -> bool:
    return bool(_GIT_URL_RE.match(target))


def _git(args: list[str], cwd: Path) -> str | None:
    """Run one git command, returning stripped stdout or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _https_url(url: str) -> str | None:
    """Normalise any git remote form to a plain https URL, or None if unknown."""
    url = url.strip()
    if url.startswith("git@"):  # git@host:org/repo.git
        host, _, path = url[4:].partition(":")
        if host and path:
            return f"https://{host}/{path.removesuffix('.git')}"
        return None
    if url.startswith("ssh://"):  # ssh://git@host/org/repo.git
        rest = url[len("ssh://"):]
        if rest.startswith("git@"):
            rest = rest[4:]
        host, _, path = rest.partition("/")
        if host and path:
            return f"https://{host}/{path.removesuffix('.git')}"
        return None
    if url.startswith(("http://", "https://")):
        return url.removesuffix(".git")
    if url.startswith("git://"):
        return "https://" + url[len("git://"):].removesuffix(".git")
    return None


def _repo_name_from_url(url: str) -> str:
    """``org/repo`` from any supported git URL form."""
    cleaned = url.strip()
    if cleaned.startswith("git@"):
        cleaned = cleaned.split(":", 1)[-1]
    else:
        cleaned = re.sub(r"^[a-z+]+://", "", cleaned)
        if cleaned.startswith("git@"):
            cleaned = cleaned[4:]
        cleaned = cleaned.split("/", 1)[-1] if "/" in cleaned else cleaned
    cleaned = cleaned.strip("/").removesuffix(".git")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else url


def _parse_tree(root: Path) -> list[ParsedLockfile]:
    parsed: list[ParsedLockfile] = []
    for lockfile in discover_lockfiles(root):
        try:
            parsed.append(parse_lockfile(lockfile, root))
        except (ValueError, OSError) as exc:
            # One malformed lockfile must not sink the whole scan.
            logger.warning("skipping unparseable lockfile %s: %s", lockfile, exc)
    return parsed


def scan_source(target: str, workdir: Path | None = None) -> RepoScan:
    """Scan a local directory or git URL into a :class:`RepoScan`."""
    if _is_git_url(target):
        return _scan_git_url(target, workdir)

    root = Path(target).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"scan target is not a directory or git URL: {target}")

    commit = _git(["rev-parse", "HEAD"], root)
    origin = _git(["config", "--get", "remote.origin.url"], root)
    return RepoScan(
        source=target,
        repo_name=root.name,
        repo_url=_https_url(origin) if origin else None,
        commit_hash=commit,
        lockfiles=_parse_tree(root),
    )


def _scan_git_url(url: str, workdir: Path | None) -> RepoScan:
    if workdir is not None:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    # mkdtemp gives a unique, empty directory; git clone accepts an empty target.
    clone_dir = Path(tempfile.mkdtemp(prefix="radix-scan-", dir=workdir))
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone --depth 1 failed for {url}: {proc.stderr.strip()[:400]}"
            )
        commit = _git(["rev-parse", "HEAD"], clone_dir)
        return RepoScan(
            source=url,
            repo_name=_repo_name_from_url(url),
            repo_url=_https_url(url),
            commit_hash=commit,
            lockfiles=_parse_tree(clone_dir),
        )
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
