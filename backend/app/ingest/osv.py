"""OSV.dev client: which advisories touch which packages.

Network-only module - it produces :class:`Advisory` and never touches HydraDB.

* ``POST /v1/querybatch`` maps packages to vulnerability *ids* (chunks of
  ≤1000 queries; per-query pagination via ``next_page_token``).
* ``GET /v1/vulns/{id}`` hydrates one advisory, disk-cached under
  ``data/cache/osv/`` so the watcher loop re-reads records from disk instead
  of re-downloading the corpus every interval.

Mapping rules (ingest contract): ``MAL-`` ids mark the OpenSSF
malicious-packages corpus and set ``malicious=True``; SEMVER range events
become ``(introduced, fixed)`` pairs; enumerated ``affected[].versions``
become ``affected_versions``; ``database_specific.severity`` is kept when
present. One OSV record can span several packages/ecosystems, so ``affected``
entries are filtered to the queried ecosystem+package before mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Iterable

import requests

from .model import Advisory

__all__ = ["OsvClient"]

_OSV_API = "https://api.osv.dev/v1"

# backend/app/ingest/osv.py -> repo root is three levels up.
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache" / "osv"

_TIMEOUT_S = 15.0
_RETRIES = 3
_BATCH_LIMIT = 1000
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    json_body: dict | None = None,
) -> requests.Response | None:
    """One HTTP call with exponential backoff on 429/5xx (3 tries total)."""
    delay = 1.0
    response: requests.Response | None = None
    for attempt in range(_RETRIES):
        try:
            response = session.request(method, url, json=json_body, timeout=_TIMEOUT_S)
        except requests.RequestException:
            response = None
        if response is not None and response.status_code not in _RETRYABLE:
            return response
        if attempt < _RETRIES - 1:
            wait = delay
            if response is not None:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
            time.sleep(min(wait, 30.0))
            delay *= 2
    return response


def _cache_filename(vuln_id: str) -> str:
    """Collision-safe filename for an advisory id (ids are near-safe already)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "__", vuln_id)
    digest = hashlib.sha1(vuln_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}.json"


def _pairs_from_events(events: list[dict]) -> list[tuple[str | None, str | None]]:
    """Fold an OSV SEMVER event list into ``(introduced, fixed)`` pairs.

    OSV guarantees events sorted by version within a range. A ``last_affected``
    event closes its window with ``fixed=None`` - the IR has no
    "last affected" slot, and an open upper bound overstates exposure rather
    than understating it, which is the right failure direction for a sentinel.
    """
    pairs: list[tuple[str | None, str | None]] = []
    introduced: str | None = None
    open_window = False
    for event in events:
        if "introduced" in event:
            if open_window:
                pairs.append((introduced, None))
            introduced = event["introduced"]
            open_window = True
        elif "fixed" in event:
            pairs.append((introduced if open_window else None, event["fixed"]))
            introduced, open_window = None, False
        elif "last_affected" in event:
            pairs.append((introduced if open_window else None, None))
            introduced, open_window = None, False
    if open_window:
        pairs.append((introduced, None))
    return pairs


def _to_advisory(record: dict, ecosystem: str | None, package: str | None) -> Advisory | None:
    """Map one raw OSV record to the trimmed :class:`Advisory` IR.

    When ``package`` is given, only ``affected`` entries for that
    ecosystem+package contribute versions/ranges; otherwise the record's first
    ``affected`` entry names the package.
    """
    vuln_id = record.get("id")
    if not vuln_id:
        return None

    affected = [entry for entry in record.get("affected") or [] if isinstance(entry, dict)]
    if package is not None:
        matches = [
            entry
            for entry in affected
            if (entry.get("package") or {}).get("name") == package
            and (
                ecosystem is None
                or ((entry.get("package") or {}).get("ecosystem") or "").lower() == ecosystem.lower()
            )
        ]
    else:
        matches = affected[:1]
        first_pkg = (matches[0].get("package") or {}) if matches else {}
        package = first_pkg.get("name") or "unknown"
        ecosystem = first_pkg.get("ecosystem") or ecosystem or "npm"

    versions: list[str] = []
    seen_versions: set[str] = set()
    ranges: list[tuple[str | None, str | None]] = []
    for entry in matches:
        for version in entry.get("versions") or []:
            if version not in seen_versions:
                seen_versions.add(version)
                versions.append(version)
        for rng in entry.get("ranges") or []:
            if rng.get("type") == "SEMVER":
                ranges.extend(_pairs_from_events(rng.get("events") or []))

    summary = record.get("summary")
    if not summary:
        details = record.get("details") or ""
        summary = details.strip().splitlines()[0][:200] if details.strip() else None

    return Advisory(
        id=str(vuln_id),
        package=package,
        ecosystem=ecosystem or "npm",
        summary=summary,
        severity=(record.get("database_specific") or {}).get("severity"),
        malicious=str(vuln_id).startswith("MAL-"),
        published=record.get("published"),
        modified=record.get("modified"),
        affected_versions=versions,
        affected_ranges=ranges,
        aliases=list(record.get("aliases") or []),
    )


class OsvClient:
    """Client for api.osv.dev with a disk cache for hydrated records."""

    def __init__(self, cache_dir: Path | None = None, user_agent: str = "radix-ingest"):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})

    # -- batch id lookup ---------------------------------------------------

    def query_batch(self, packages: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
        """Vulnerability ids per package.

        Keys are ``f"{ecosystem}:{name}"`` - the same canonical form as
        ``schema.package_key`` - because the contract types the mapping as
        ``dict[str, list[str]]``. Every queried package gets a key; a clean
        package maps to ``[]``. A chunk that still fails after retries is
        skipped (its packages simply stay empty) - advisory lookup never
        crashes the pipeline.
        """
        ordered: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for ecosystem, name in packages:
            key = (ecosystem, name)
            if key not in seen:
                seen.add(key)
                ordered.append(key)

        results: dict[str, list[str]] = {f"{eco}:{name}": [] for eco, name in ordered}

        # (ecosystem, name, page_token) - paginated queries are re-queued with
        # their next_page_token until OSV stops handing tokens back.
        queue: list[tuple[str, str, str | None]] = [(eco, name, None) for eco, name in ordered]
        while queue:
            chunk, queue = queue[:_BATCH_LIMIT], queue[_BATCH_LIMIT:]
            body = {
                "queries": [
                    {
                        "package": {"name": name, "ecosystem": ecosystem},
                        **({"page_token": token} if token else {}),
                    }
                    for ecosystem, name, token in chunk
                ]
            }
            response = _request_with_retry(self._session, "POST", f"{_OSV_API}/querybatch", body)
            if response is None or response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue

            for (ecosystem, name, _token), result in zip(chunk, payload.get("results") or []):
                result = result or {}
                key = f"{ecosystem}:{name}"
                for vuln in result.get("vulns") or []:
                    vuln_id = vuln.get("id")
                    if vuln_id and vuln_id not in results[key]:
                        results[key].append(vuln_id)
                next_token = result.get("next_page_token")
                if next_token:
                    queue.append((ecosystem, name, next_token))
        return results

    # -- hydration ---------------------------------------------------------

    def get_advisory(
        self,
        vuln_id: str,
        *,
        ecosystem: str | None = None,
        package: str | None = None,
    ) -> Advisory | None:
        """One advisory by id, mapped to the IR; ``None`` when unavailable.

        ``ecosystem``/``package`` scope the ``affected[]`` entries to the
        package that surfaced the id - an OSV record can span packages.
        Called bare (contract signature), the record's first ``affected``
        entry names the package.
        """
        record = self._get_raw(vuln_id)
        if record is None:
            return None
        return _to_advisory(record, ecosystem, package)

    def _get_raw(self, vuln_id: str) -> dict | None:
        """The raw OSV record, from disk when cached, else fetched and cached."""
        path = self.cache_dir / _cache_filename(vuln_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("id"):
                return record
        except (OSError, ValueError):
            pass  # absent or corrupt cache entry - refetch below

        url = f"{_OSV_API}/vulns/{urllib.parse.quote(vuln_id, safe='')}"
        response = _request_with_retry(self._session, "GET", url)
        if response is None or response.status_code != 200:
            return None
        try:
            record = response.json()
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        try:
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written is a slow cache, not an error
        return record

    # -- the one-call form the writer and watcher use ----------------------

    def advisories_for(self, packages: Iterable[tuple[str, str]]) -> list[Advisory]:
        """All advisories touching the given packages, hydrated once per id.

        Ids surfacing under several queried packages are deduplicated - the
        first package that surfaced an id is the one its ``affected[]``
        entries are scoped to.
        """
        ordered: list[tuple[str, str]] = []
        seen_pkg: set[tuple[str, str]] = set()
        for ecosystem, name in packages:
            key = (ecosystem, name)
            if key not in seen_pkg:
                seen_pkg.add(key)
                ordered.append(key)

        ids_by_key = self.query_batch(ordered)
        advisories: list[Advisory] = []
        seen_ids: set[str] = set()
        for ecosystem, name in ordered:
            for vuln_id in ids_by_key.get(f"{ecosystem}:{name}", []):
                if vuln_id in seen_ids:
                    continue
                seen_ids.add(vuln_id)
                advisory = self.get_advisory(vuln_id, ecosystem=ecosystem, package=name)
                if advisory is not None:
                    advisories.append(advisory)
        return advisories
