"""Live verification for NpmRegistry and OsvClient.

These tests hit the real registry.npmjs.org, api.npmjs.org and api.osv.dev -
per the ingest contract, first proof is against the real thing, not fixtures.
They need the network, and populate the real disk caches under
``data/cache/{registry,osv}/`` (that is the point of test 6).

Runnable two ways:
  * pytest backend/tests/test_registry_osv_live.py
  * backend/.venv/bin/python backend/tests/test_registry_osv_live.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.model import PackageMeta  # noqa: E402
from app.ingest.osv import OsvClient  # noqa: E402
from app.ingest.registry import NpmRegistry  # noqa: E402


def test_get_meta_react():
    """Full-doc metadata for an unscoped package: maintainers, time map, latest."""
    registry = NpmRegistry()
    meta = registry.get_meta("react")
    assert meta is not None, "get_meta('react') returned None"
    assert meta.name == "react"

    usernames = [m.username for m in meta.maintainers]
    assert usernames, "react must list maintainers"
    assert "fb" in usernames, f"expected 'fb' among react maintainers, got {usernames}"

    assert meta.latest, "dist-tags.latest missing"
    assert meta.latest[0].isdigit()

    assert len(meta.published_at) > 1000, "full doc should carry the whole time map"
    stamp = meta.published_at.get("18.2.0")
    assert stamp and stamp.startswith("2022-06-14"), f"react@18.2.0 publish time wrong: {stamp}"
    assert "created" not in meta.published_at and "modified" not in meta.published_at

    print(f"  react: latest={meta.latest} maintainers={usernames} "
          f"time-map={len(meta.published_at)} versions, 18.2.0 published {stamp}")


def test_get_meta_scoped():
    """Scoped names must URL-encode and cache under a sanitised filename."""
    registry = NpmRegistry()
    meta = registry.get_meta("@tanstack/react-query")
    assert meta is not None, "get_meta('@tanstack/react-query') returned None"
    assert meta.name == "@tanstack/react-query"
    assert meta.maintainers, "scoped package must list maintainers"
    assert meta.latest and meta.published_at
    # the cache file for the scoped name must exist and contain no '/'
    cached = [p.name for p in registry.cache_dir.iterdir() if p.name.startswith("@tanstack__")]
    assert cached, "sanitised cache file for scoped name not written"
    print(f"  @tanstack/react-query: latest={meta.latest} "
          f"maintainers={[m.username for m in meta.maintainers]} cache-file={cached[0]}")


def test_fill_downloads():
    """Bulk endpoint for unscoped, per-name for scoped, None for unknowns."""
    registry = NpmRegistry()
    names = ["react", "lodash", "@tanstack/react-query", "radix-no-such-pkg-xyz-123"]
    metas = {name: PackageMeta(name=name) for name in names}
    registry.fill_downloads(metas)

    assert metas["react"].downloads_weekly and metas["react"].downloads_weekly > 10_000_000
    assert metas["lodash"].downloads_weekly and metas["lodash"].downloads_weekly > 10_000_000
    scoped = metas["@tanstack/react-query"].downloads_weekly
    assert scoped and scoped > 1_000_000, f"scoped downloads path failed: {scoped}"
    assert metas["radix-no-such-pkg-xyz-123"].downloads_weekly is None

    for name in names:
        print(f"  downloads/week {name}: {metas[name].downloads_weekly}")


def test_event_stream_incident():
    """The 2018 event-stream incident must surface, MAL- handling included.

    The GHSA record spans BOTH event-stream and flatmap-stream, so this also
    proves (a) dedupe across packages - the shared id is hydrated once - and
    (b) the affected[] filter - its versions are event-stream's 3.3.6, not
    flatmap-stream's 0.1.1.
    """
    osv = OsvClient()
    advisories = osv.advisories_for([("npm", "event-stream"), ("npm", "flatmap-stream")])
    assert advisories, "no advisories for the event-stream incident"

    ids = [a.id for a in advisories]
    assert len(ids) == len(set(ids)), f"duplicate ids across packages: {ids}"
    assert any(a.id.startswith("GHSA-") for a in advisories), f"no GHSA record in {ids}"
    assert any(a.malicious and a.id.startswith("MAL-") for a in advisories), \
        f"no MAL- record (malicious=True) in {ids}"

    es = [a for a in advisories if a.package == "event-stream"]
    assert es, "no advisory attributed to event-stream itself"
    assert any(
        "3.3.6" in a.affected_versions or ("3.3.6", "4.0.0") in a.affected_ranges for a in es
    ), "event-stream@3.3.6 not flagged"

    for a in advisories:
        print(f"  {a.id} pkg={a.package} malicious={a.malicious} severity={a.severity}")
        print(f"    summary: {(a.summary or '')[:100]}")
        print(f"    versions={a.affected_versions[:6]} ranges={a.affected_ranges}")


def test_lodash_ranges():
    """Ordinary vulns: several GHSAs with (introduced, fixed) pairs populated."""
    osv = OsvClient()
    advisories = osv.advisories_for([("npm", "lodash")])
    ghsas = [a for a in advisories if a.id.startswith("GHSA-")]
    assert len(ghsas) >= 5, f"expected several lodash GHSAs, got {len(ghsas)}"
    assert all(not a.malicious for a in ghsas)

    with_fixed = [a for a in ghsas if any(fixed for _intro, fixed in a.affected_ranges)]
    assert len(with_fixed) >= 5, "expected (introduced, fixed) pairs on lodash GHSAs"
    assert any(a.severity for a in ghsas), "database_specific.severity never present"

    print(f"  lodash: {len(advisories)} advisories, {len(with_fixed)} with fixed ranges")
    for a in ghsas[:5]:
        print(f"  {a.id} severity={a.severity} ranges={a.affected_ranges} "
              f"| {(a.summary or '')[:60]}")


def test_etag_cache_revalidation():
    """A re-fetch must revalidate via If-None-Match (304), not re-download MBs."""
    registry = NpmRegistry()
    first = registry.get_meta("react")
    assert first is not None
    first_mode = registry.last_fetch  # "network" on a cold cache, else "revalidated"

    started = time.perf_counter()
    second = registry.get_meta("react")
    elapsed = time.perf_counter() - started
    assert second is not None
    assert registry.last_fetch == "revalidated", \
        f"expected an ETag 304 revalidation, got {registry.last_fetch!r}"
    assert second.latest == first.latest
    assert len(second.published_at) == len(first.published_at)

    cache_file = next(p for p in registry.cache_dir.iterdir() if p.name.startswith("react-"))
    print(f"  first={first_mode} second={registry.last_fetch} in {elapsed * 1000:.0f}ms; "
          f"cache file {cache_file.name} = {cache_file.stat().st_size} bytes "
          f"(full doc is multi-MB)")


_TESTS = (
    test_get_meta_react,
    test_get_meta_scoped,
    test_fill_downloads,
    test_event_stream_incident,
    test_lodash_ranges,
    test_etag_cache_revalidation,
)


if __name__ == "__main__":
    failures = 0
    for test in _TESTS:
        print(f"[ RUN ] {test.__name__}")
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL ] {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a live test must name what broke
            failures += 1
            print(f"[ERROR] {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"[ OK  ] {test.__name__}")
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    sys.exit(1 if failures else 0)
