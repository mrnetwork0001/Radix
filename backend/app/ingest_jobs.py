"""In-process ingestion job runner for the console's "ADD REPO" flow.

One :class:`IngestJobStore` lives on the FastAPI app; the orchestrator wires
``POST /api/ingest`` to :meth:`IngestJobStore.start` and
``GET /api/ingest/{job_id}`` to :meth:`IngestJobStore.get`.

Design constraints, in order:

* **One job at a time.** Ingestion is registry- and OSV-heavy (hundreds of
  metadata fetches on a cold cache), so a second ``start()`` while a job is
  running raises :class:`JobBusyError` naming the active job rather than
  queueing silently.
* **The job dict is the frozen ``IngestJob`` shape** from
  ``frontend/src/lib/types.ts``: ``job_id``, ``status`` in
  ``running | done | error``, append-only ``log``, ``target``, plus ``report``
  when done and ``error`` when failed. ``get()`` returns a snapshot copy so
  the route can serialise it without racing the worker thread.
* **Never crash.** Any exception in the worker becomes ``status: "error"``
  with a human string; the store survives and accepts the next job.
* **The pipeline is the existing one** - ``app.ingest.repo.scan_source`` ->
  npm registry enrichment -> OSV advisories ->
  ``app.ingest.graph_writer.GraphWriter.ingest`` - and the progress lines
  mirror the phrasing of ``scripts/ingest.py`` so the terminal feed in the UI
  reads like the CLI does.

The pipeline pieces are injectable (``scan_source`` and the three factories)
so unit tests can run the whole job lifecycle with fakes, and callers can skip
registry/OSV enrichment by passing a factory that returns ``None`` - exactly
like the CLI's ``--no-registry`` / ``--no-osv`` flags.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Protocol

__all__ = ["IngestJobStore", "JobBusyError"]

# Mirrors app.ingest.repo._GIT_URL_RE: what scan_source will treat as a clone.
_GIT_URL_RE = re.compile(r"^(https?://|git://|ssh://|git@)")

#: Finished (done or error) jobs kept for polling stragglers; the oldest are
#: pruned beyond this. A running job is never pruned.
MAX_FINISHED_JOBS = 20

#: Repo root (this file is backend/app/ingest_jobs.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "cache"


class JobBusyError(RuntimeError):
    """A second start() while a job is running; message names the active job."""

    def __init__(self, active_job_id: str) -> None:
        super().__init__(
            f"an ingestion job is already running (job {active_job_id}); "
            "wait for it to finish and retry"
        )
        self.active_job_id = active_job_id


class _Writer(Protocol):
    def ingest(self, scan: Any, meta: dict[str, Any], advisories: list[Any]) -> Any: ...


def _default_scan_source(target: str) -> Any:
    from app.ingest.repo import scan_source

    return scan_source(target)


def _default_writer_factory(namespace: str) -> _Writer:
    from app.hydra_client import HydraClient
    from app.ingest.graph_writer import GraphWriter

    return GraphWriter(HydraClient(namespace=namespace))


def _default_registry_factory() -> Any:
    from app.ingest.registry import NpmRegistry

    return NpmRegistry(cache_dir=_DEFAULT_CACHE_DIR / "registry")


def _default_osv_factory() -> Any:
    from app.ingest.osv import OsvClient

    return OsvClient(cache_dir=_DEFAULT_CACHE_DIR / "osv")


def _validate_target(target: str) -> str:
    """Non-empty, and either a git URL or an existing local directory."""
    cleaned = (target or "").strip()
    if not cleaned:
        raise ValueError("target must be a git URL or a local directory path")
    if _GIT_URL_RE.match(cleaned):
        return cleaned
    if Path(cleaned).expanduser().is_dir():
        return cleaned
    raise ValueError(
        f"target is neither a git URL (https/ssh) nor an existing local directory: {cleaned}"
    )


def _validate_namespace(namespace: str) -> str:
    cleaned = (namespace or "").strip()
    if not cleaned:
        raise ValueError("namespace must be non-empty")
    if cleaned == "radix":
        # The seeded demo world carries fabricated compromise flags; mixing
        # real data in would corrupt both (same guard as scripts/ingest.py).
        raise ValueError(
            "the demo namespace 'radix' is read-only; ingest into 'radix/live' "
            "or another 'radix/<scope>' sub-namespace"
        )
    return cleaned


class IngestJobStore:
    """Thread-safe, single-worker job store for repository ingestion."""

    def __init__(
        self,
        *,
        scan_source: Callable[[str], Any] | None = None,
        writer_factory: Callable[[str], _Writer] | None = None,
        registry_factory: Callable[[], Any] | None = None,
        osv_factory: Callable[[], Any] | None = None,
        max_finished: int = MAX_FINISHED_JOBS,
    ) -> None:
        self._scan_source = scan_source or _default_scan_source
        self._writer_factory = writer_factory or _default_writer_factory
        self._registry_factory = registry_factory or _default_registry_factory
        self._osv_factory = osv_factory or _default_osv_factory
        self._max_finished = max_finished

        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._active_job_id: str | None = None

    # -- public API --------------------------------------------------------

    def start(self, target: str, namespace: str) -> str:
        """Validate, register and launch one job; returns its ``job_id``.

        Raises :class:`JobBusyError` while another job is running and
        :class:`ValueError` for an invalid target or namespace.
        """
        cleaned_target = _validate_target(target)
        cleaned_namespace = _validate_namespace(namespace)

        with self._lock:
            if self._active_job_id is not None:
                raise JobBusyError(self._active_job_id)
            job_id = uuid.uuid4().hex[:12]
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "log": [],
                "target": cleaned_target,
            }
            self._active_job_id = job_id
            self._prune_locked()

        worker = threading.Thread(
            target=self._run_job,
            args=(job_id, cleaned_target, cleaned_namespace),
            name=f"radix-ingest-{job_id}",
            daemon=True,
        )
        worker.start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Snapshot of one job in the frozen ``IngestJob`` shape, or ``None``."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            snapshot = dict(job)
            snapshot["log"] = list(job["log"])
            if "report" in job:
                snapshot["report"] = dict(job["report"])
            return snapshot

    # -- worker ------------------------------------------------------------

    def _log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["log"].append(line)

    def _finish(self, job_id: str, *, report: dict[str, Any] | None, error: str | None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                if error is not None:
                    job["status"] = "error"
                    job["error"] = error
                else:
                    job["status"] = "done"
                    job["report"] = report or {}
            if self._active_job_id == job_id:
                self._active_job_id = None
            self._prune_locked()

    def _prune_locked(self) -> None:
        """Drop the oldest finished jobs beyond ``max_finished``. Lock held."""
        finished = [jid for jid, job in self._jobs.items() if job["status"] != "running"]
        for jid in finished[: max(0, len(finished) - self._max_finished)]:
            del self._jobs[jid]

    def _run_job(self, job_id: str, target: str, namespace: str) -> None:
        log = lambda line: self._log(job_id, line)  # noqa: E731 - local shorthand
        started = time.perf_counter()
        try:
            # Writer first: it label-scans the namespace at init, so a bad
            # namespace or an unreachable HydraDB fails fast, before any
            # expensive clone or network enrichment.
            writer = self._writer_factory(namespace)

            log(f"── scanning {target}")
            scan = self._scan_source(target)

            if not scan.lockfiles:
                log("   no lockfiles found - nothing to ingest")
                log(
                    f"── done in {time.perf_counter() - started:.1f}s "
                    f"(namespace: {namespace})"
                )
                self._finish(job_id, report=_empty_report(), error=None)
                return

            for lf in scan.lockfiles:
                log(f"   {lf.path}: {len(lf.releases)} releases, {len(lf.edges)} edges ({lf.kind})")

            names = sorted({r.name for lf in scan.lockfiles for r in lf.releases})

            metas: dict[str, Any] = {}
            registry = self._registry_factory()
            if registry is not None:
                log(f"   registry: enriching {len(names)} packages…")
                for name in names:
                    meta = registry.get_meta(name)
                    if meta is not None:
                        metas[name] = meta
                registry.fill_downloads(metas)
                log(f"   registry: {len(metas)}/{len(names)} enriched")
            else:
                log("   registry: enrichment skipped")

            advisories: list[Any] = []
            osv = self._osv_factory()
            if osv is not None:
                log(f"   osv: querying {len(names)} packages…")
                advisories = osv.advisories_for([("npm", n) for n in names])
                malicious = sum(1 for a in advisories if a.malicious)
                log(f"   osv: {len(advisories)} advisories ({malicious} malicious)")
            else:
                malicious = 0
                log("   osv: advisory sync skipped")

            report = writer.ingest(scan, metas, advisories)
            log(
                f"   wrote: {report.packages} packages, {report.versions} versions, "
                f"{report.maintainers} maintainers, {report.services} services, "
                f"{report.lockfiles} lockfiles | {report.depends_on} DEPENDS_ON, "
                f"{report.resolved_in} RESOLVED_IN, {report.typosquats} typosquats | "
                f"{report.compromised_marked} compromised | "
                f"{report.statements} statements, {report.wire_seconds:.2f}s on the wire"
            )
            log(f"── done in {time.perf_counter() - started:.1f}s (namespace: {namespace})")

            self._finish(
                job_id,
                report={
                    "packages": report.packages,
                    "versions": report.versions,
                    "maintainers": report.maintainers,
                    "services": report.services,
                    "lockfiles": report.lockfiles,
                    "depends_on": report.depends_on,
                    "typosquats": report.typosquats,
                    "compromised_marked": report.compromised_marked,
                    "advisories": len(advisories),
                    "malicious": malicious,
                },
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - the job must never crash the store
            message = str(exc).strip() or type(exc).__name__
            log(f"   error: {message}")
            self._finish(job_id, report=None, error=message)


def _empty_report() -> dict[str, Any]:
    """The frozen report shape, all zeroes - for a target with no lockfiles."""
    return {
        "packages": 0,
        "versions": 0,
        "maintainers": 0,
        "services": 0,
        "lockfiles": 0,
        "depends_on": 0,
        "typosquats": 0,
        "compromised_marked": 0,
        "advisories": 0,
        "malicious": 0,
    }
