"""Radix sentinel — polls OSV for new advisories against every package in the graph.

    python -m sentinel.watcher --interval 900 [--once] [--dry-run]

Each cycle: enumerate Package names in the namespace, ask OSV for their
advisories, apply them through the graph writer, and log a one-line delta
(``+2 compromised, +3 versions windowed``). All state lives in HydraDB, so the
process is stateless and safe to restart at any moment.

Operational behaviour:

* Structured, timestamped logging to stdout — journald/`docker logs` friendly.
* A failed cycle never kills the loop; retries back off exponentially
  (30 s → 1 h cap) and reset on the next success.
* SIGTERM/SIGINT exit cleanly, even mid-sleep.
* ``--dry-run`` only *reads* the namespace and prints what it would query. It
  deliberately avoids importing the ingestion modules, so it works while
  ``app/ingest/osv.py`` / ``app/ingest/graph_writer.py`` are still landing.

Config via env (see ``docs/INGEST_CONTRACT.md``): ``HYDRA_HTTP_URL``,
``HYDRA_TOKEN``, ``HYDRA_NAMESPACE``, ``SENTINEL_INTERVAL``, and optional
``SENTINEL_REPOS`` — a comma-separated list of paths/git URLs re-scanned every
cycle through the full ingest pipeline.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

# The backend package is importable as `app` from two layouts: a repo checkout
# (<root>/backend/app) and the deploy image (/app/app, where WORKDIR=/app).
_PKG_DIR = Path(__file__).resolve().parent
for _candidate in (_PKG_DIR.parent, _PKG_DIR.parent / "backend"):
    if (_candidate / "app" / "hydra_client.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from app import schema  # noqa: E402
from app.hydra_client import HydraClient  # noqa: E402

if TYPE_CHECKING:  # real imports happen lazily inside main()
    from app.ingest.graph_writer import GraphWriter
    from app.ingest.model import Advisory
    from app.ingest.osv import OsvClient

log = logging.getLogger("radix.sentinel")

DEFAULT_INTERVAL_S = 900
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 3600.0


def _configure_logging() -> None:
    """One-line UTC-timestamped records on stdout, no colour, no buffering."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler])


class _Shutdown:
    """Signal-driven stop flag whose ``wait`` doubles as an interruptible sleep."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.signal_name: str | None = None

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)

    def _handle(self, signum: int, _frame: object) -> None:
        self.signal_name = signal.Signals(signum).name
        self._event.set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True means a stop signal cut it short."""
        return self._event.wait(seconds)

    @property
    def requested(self) -> bool:
        return self._event.is_set()


# --------------------------------------------------------------------------
# Graph reads
# --------------------------------------------------------------------------


def enumerate_packages(client: HydraClient) -> list[tuple[str, str]]:
    """Every ``(ecosystem, name)`` stored in the namespace, sorted and deduped."""
    result = client.execute(
        f"MATCH (n:{schema.PACKAGE}) RETURN n.name AS name, n.ecosystem AS ecosystem"
    )
    return sorted(
        {(row.get("ecosystem") or "npm", row["name"]) for row in result.rows if row.get("name")}
    )


def threat_counts(client: HydraClient) -> tuple[int, int]:
    """``(compromised packages, windowed versions)`` — the delta line's basis."""
    compromised = client.execute(
        f"MATCH (n:{schema.PACKAGE}) WHERE n.is_compromised = true RETURN count(*) AS c"
    )
    windowed = client.execute(
        f"MATCH (n:{schema.VERSION}) WHERE n.compromised_window = true RETURN count(*) AS c"
    )
    return int(compromised.scalar("c", 0) or 0), int(windowed.scalar("c", 0) or 0)


# --------------------------------------------------------------------------
# Cycles
# --------------------------------------------------------------------------


def run_cycle(client: HydraClient, osv_client: "OsvClient", writer: "GraphWriter") -> None:
    started = time.monotonic()
    packages = enumerate_packages(client)
    if not packages:
        log.info(
            "namespace %r holds no packages — nothing to query (seed or ingest first)",
            client.config.namespace,
        )
        return

    before_compromised, before_windowed = threat_counts(client)
    advisories: list[Advisory] = osv_client.advisories_for(packages)
    malicious = sum(1 for advisory in advisories if advisory.malicious)
    applied = writer.apply_advisories(advisories)
    after_compromised, after_windowed = threat_counts(client)

    delta = (
        f"{after_compromised - before_compromised:+d} compromised, "
        f"{after_windowed - before_windowed:+d} versions windowed"
    )
    log.info(
        'cycle complete: packages=%d advisories=%d malicious=%d applied=%d delta="%s" elapsed=%.1fs',
        len(packages),
        len(advisories),
        malicious,
        applied,
        delta,
        time.monotonic() - started,
    )


def run_dry_cycle(client: HydraClient) -> None:
    """Read-only preview: enumerate the namespace, print what OSV would be asked."""
    started = time.monotonic()
    packages = enumerate_packages(client)
    preview = ", ".join(f"{eco}:{name}" for eco, name in packages[:8])
    if len(packages) > 8:
        preview += f", … ({len(packages) - 8} more)"
    log.info(
        "dry-run: would query OSV (api.osv.dev/v1/querybatch) for %d packages "
        "in namespace %r: %s",
        len(packages),
        client.config.namespace,
        preview or "<none>",
    )
    log.info(
        "dry-run cycle complete: packages=%d writes=0 elapsed=%.1fs",
        len(packages),
        time.monotonic() - started,
    )


def rescan_repos(
    targets: list[str],
    osv_client: "OsvClient",
    writer: "GraphWriter",
    shutdown: _Shutdown,
) -> None:
    """Optional per-cycle re-ingest of the repos named in ``SENTINEL_REPOS``.

    Runs the full contract pipeline (scan → registry → OSV → write) per target;
    a failing target is logged and skipped so the advisory loop never suffers.
    """
    from app.ingest.registry import NpmRegistry
    from app.ingest.repo import scan_source

    registry = NpmRegistry(user_agent="radix-sentinel")
    for target in targets:
        if shutdown.requested:
            return
        try:
            scan = scan_source(target)
            names = sorted(
                {rel.name for lockfile in scan.lockfiles for rel in lockfile.releases}
            )
            metas = {}
            for name in names:
                meta = registry.get_meta(name)
                if meta is not None:
                    metas[name] = meta
            registry.fill_downloads(metas)
            packages = sorted(
                {
                    (rel.ecosystem, rel.name)
                    for lockfile in scan.lockfiles
                    for rel in lockfile.releases
                }
            )
            advisories = osv_client.advisories_for(packages)
            report = writer.ingest(scan, metas, advisories)
            log.info(
                "re-scan %s: packages=%d versions=%d depends_on=%d compromised_marked=%d",
                target,
                report.packages,
                report.versions,
                report.depends_on,
                report.compromised_marked,
            )
        except Exception:
            log.exception("re-scan failed for %s (skipped)", target)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.watcher",
        description="Radix 24/7 sentinel: poll OSV and apply advisories to the graph.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("SENTINEL_INTERVAL", str(DEFAULT_INTERVAL_S))),
        metavar="SECONDS",
        help="seconds between cycles (default: $SENTINEL_INTERVAL or %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read-only: enumerate packages and print what would be queried",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging()

    shutdown = _Shutdown()
    shutdown.install()

    client = HydraClient()  # env-configured: HYDRA_HTTP_URL / HYDRA_TOKEN / HYDRA_NAMESPACE
    log.info(
        "sentinel starting: hydra=%s namespace=%r interval=%ds mode=%s",
        client.config.http_url,
        client.config.namespace,
        args.interval,
        "dry-run" if args.dry_run else "live",
    )

    osv_client = writer = None
    rescan_targets: list[str] = []
    if not args.dry_run:
        # Lazy: these modules are separate deliverables. Failing here with a
        # named path beats an ImportError traceback five frames deep.
        try:
            from app.ingest.graph_writer import GraphWriter
            from app.ingest.osv import OsvClient
        except ImportError as exc:
            log.error(
                "ingestion modules unavailable (%s). Live watching needs "
                "backend/app/ingest/osv.py and backend/app/ingest/graph_writer.py; "
                "until they exist, run with --dry-run to preview enumeration.",
                exc,
            )
            return 2
        osv_client = OsvClient()
        writer = GraphWriter(client)
        rescan_targets = [
            target.strip() for target in os.getenv("SENTINEL_REPOS", "").split(",") if target.strip()
        ]
        if rescan_targets:
            log.info("SENTINEL_REPOS: re-scanning %d target(s) each cycle", len(rescan_targets))

    failures = 0
    exit_code = 0
    while True:
        try:
            if args.dry_run:
                run_dry_cycle(client)
            else:
                if rescan_targets:
                    rescan_repos(rescan_targets, osv_client, writer, shutdown)
                run_cycle(client, osv_client, writer)
            failures = 0
            exit_code = 0
        except Exception:
            failures += 1
            exit_code = 1
            log.exception("cycle failed (consecutive failures: %d)", failures)

        if args.once or shutdown.requested:
            break

        if failures:
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (failures - 1)))
            log.info("backing off %.0fs before retry", delay)
        else:
            delay = float(args.interval)
        if shutdown.wait(delay):
            break

    if shutdown.signal_name:
        log.info("received %s — exiting cleanly", shutdown.signal_name)
    client.close()
    return exit_code if args.once else 0


if __name__ == "__main__":
    raise SystemExit(main())
