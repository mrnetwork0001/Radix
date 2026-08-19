"""npm registry client: package metadata and weekly download counts.

Network-only module - it produces :class:`PackageMeta` and never touches
HydraDB. Two endpoints:

* ``registry.npmjs.org/{name}`` - the **full** metadata document (the
  abbreviated "corgi" format lacks the ``time`` map, and per-version publish
  timestamps are the maintainer sentinel's whole feature). Multi-megabyte for
  popular packages, so responses are trimmed immediately and the trimmed form
  is cached to disk with the response ETag; re-runs revalidate with
  ``If-None-Match`` and a 304 serves the cached trim without re-downloading.
* ``api.npmjs.org/downloads/point/last-week/…`` - weekly downloads. The bulk
  comma-joined form takes up to 128 *unscoped* names per call; scoped names go
  one at a time with ``%2F`` encoding.

Failure policy per the ingest contract: one bad package must never crash the
pipeline. ``get_meta`` returns ``None`` (or the stale cache, if any) and
``fill_downloads`` leaves ``downloads_weekly`` as ``None``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

import requests

from . import paths
from .model import MaintainerInfo, PackageMeta

__all__ = ["NpmRegistry"]

_REGISTRY_URL = "https://registry.npmjs.org"
_DOWNLOADS_URL = "https://api.npmjs.org/downloads/point/last-week"

# backend/app/ingest/registry.py -> repo root is three levels up.
#: Resolved lazily by :mod:`app.ingest.paths`, which handles both the repo
#: and image layouts and degrades to a temp dir when the target is not writable.
_CACHE_NAME = "registry"

_TIMEOUT_S = 15.0
_RETRIES = 3
_BULK_LIMIT = 128
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _get_with_retry(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None = None,
) -> requests.Response | None:
    """GET with exponential backoff on 429/5xx (3 tries total).

    Returns the final response, or ``None`` when every attempt raised at the
    transport level. A response that is *still* 429/5xx after the last try is
    returned as-is so callers can distinguish "server said no" from "no
    network"; both are treated as a miss.
    """
    delay = 1.0
    response: requests.Response | None = None
    for attempt in range(_RETRIES):
        try:
            response = session.get(url, headers=headers, timeout=_TIMEOUT_S)
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


def _cache_filename(name: str) -> str:
    """A collision-safe filename for a package name.

    Scoped names contain ``/``, which cannot appear in a filename; anything
    outside a conservative character set becomes ``__``, and a short digest of
    the *original* name is appended so sanitisation can never alias two
    packages onto one cache entry.
    """
    safe = re.sub(r"[^A-Za-z0-9@._-]", "__", name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}.json"


def _trim_doc(name: str, doc: dict) -> PackageMeta:
    """Reduce a full registry document to the fields Radix stores."""
    maintainers = [
        MaintainerInfo(username=str(entry["name"]), email=entry.get("email"))
        for entry in (doc.get("maintainers") or [])
        if isinstance(entry, dict) and entry.get("name")
    ]
    published_at = {
        version: stamp
        for version, stamp in (doc.get("time") or {}).items()
        if version not in ("created", "modified")
    }
    latest = (doc.get("dist-tags") or {}).get("latest")
    return PackageMeta(
        name=doc.get("name") or name,
        maintainers=maintainers,
        published_at=published_at,
        latest=latest,
    )


class NpmRegistry:
    """Client for registry.npmjs.org metadata and api.npmjs.org downloads."""

    def __init__(self, cache_dir: Path | None = None, user_agent: str = "radix-ingest"):
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.cache_dir = paths.cache_dir(_CACHE_NAME)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                # The default Accept yields the full document; the corgi format
                # only comes back when explicitly requested. Stated anyway so a
                # registry-side default change cannot silently drop `time`.
                "Accept": "application/json",
            }
        )
        #: How the most recent ``get_meta`` call was satisfied - one of
        #: "network", "revalidated" (ETag 304), "stale-fallback",
        #: "not-found", "error", or None before the first call. Exists so
        #: tests and operators can *prove* the cache worked.
        self.last_fetch: str | None = None

    # -- metadata ----------------------------------------------------------

    def get_meta(self, name: str) -> PackageMeta | None:
        """Fetch and trim one package's registry document, or ``None``."""
        cache_path = self.cache_dir / _cache_filename(name)
        cached = self._read_cache(cache_path)

        headers: dict[str, str] = {}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        url = f"{_REGISTRY_URL}/{urllib.parse.quote(name, safe='@')}"
        response = _get_with_retry(self._session, url, headers or None)

        if response is None or response.status_code in _RETRYABLE:
            # Transport failure or the registry is still unhappy after
            # retries: stale metadata beats no metadata for a sentinel.
            if cached:
                self.last_fetch = "stale-fallback"
                return self._meta_from_cache(name, cached)
            self.last_fetch = "error"
            return None

        if response.status_code == 304 and cached:
            self.last_fetch = "revalidated"
            return self._meta_from_cache(name, cached)

        if response.status_code != 200:
            self.last_fetch = "not-found" if response.status_code == 404 else "error"
            return None

        try:
            doc = response.json()
        except ValueError:
            self.last_fetch = "error"
            return None
        if not isinstance(doc, dict):
            self.last_fetch = "error"
            return None

        meta = _trim_doc(name, doc)
        self._write_cache(cache_path, name, response.headers.get("ETag"), meta)
        self.last_fetch = "network"
        return meta

    def _read_cache(self, path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) and "meta" in payload else None

    def _write_cache(self, path: Path, name: str, etag: str | None, meta: PackageMeta) -> None:
        payload = {
            "name": name,
            "etag": etag,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "meta": {
                "name": meta.name,
                "latest": meta.latest,
                "maintainers": [
                    {"username": m.username, "email": m.email} for m in meta.maintainers
                ],
                "published_at": meta.published_at,
            },
        }
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written is a slow cache, not an error

    def _meta_from_cache(self, name: str, cached: dict) -> PackageMeta:
        stored = cached.get("meta") or {}
        maintainers = [
            MaintainerInfo(username=entry.get("username", ""), email=entry.get("email"))
            for entry in (stored.get("maintainers") or [])
            if entry.get("username")
        ]
        return PackageMeta(
            name=stored.get("name") or name,
            maintainers=maintainers,
            published_at=dict(stored.get("published_at") or {}),
            latest=stored.get("latest"),
        )

    # -- downloads ---------------------------------------------------------

    def fill_downloads(self, metas: dict[str, PackageMeta]) -> None:
        """Populate ``downloads_weekly`` in place for every meta it can.

        Unscoped names ride the bulk endpoint (≤128 per call); scoped names
        are fetched one at a time because the bulk form rejects them.
        Packages the downloads API does not know keep ``None``.
        """
        unscoped = [name for name in metas if not name.startswith("@")]
        scoped = [name for name in metas if name.startswith("@")]

        for start in range(0, len(unscoped), _BULK_LIMIT):
            self._fill_bulk(unscoped[start : start + _BULK_LIMIT], metas)

        for name in scoped:
            count = self._fetch_downloads_single(name)
            if count is not None and name in metas:
                metas[name].downloads_weekly = count

    def _fill_bulk(self, chunk: list[str], metas: dict[str, PackageMeta]) -> None:
        # A one-name "bulk" request returns the single-package shape, not a
        # map keyed by name - route it through the single fetch instead.
        if len(chunk) == 1:
            count = self._fetch_downloads_single(chunk[0])
            if count is not None:
                metas[chunk[0]].downloads_weekly = count
            return

        response = _get_with_retry(self._session, f"{_DOWNLOADS_URL}/{','.join(chunk)}")
        if response is None or response.status_code != 200:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        for name in chunk:
            entry = payload.get(name)  # unknown packages come back as null
            if isinstance(entry, dict) and isinstance(entry.get("downloads"), int):
                metas[name].downloads_weekly = entry["downloads"]

    def _fetch_downloads_single(self, name: str) -> int | None:
        url = f"{_DOWNLOADS_URL}/{urllib.parse.quote(name, safe='@')}"
        response = _get_with_retry(self._session, url)
        if response is None or response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        downloads = payload.get("downloads") if isinstance(payload, dict) else None
        return downloads if isinstance(downloads, int) else None
