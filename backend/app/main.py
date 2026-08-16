"""Radix REST API - the FastAPI surface described by ``docs/API_CONTRACT.md``.

Seven endpoints, one HydraDB connection, and one rule that shapes all of them:
``latency_ms`` is the *traversal* cost. Every handler accumulates the wire
latency reported by :mod:`app.hydra_client` and never brackets its own
serialisation, because that number is shown in the UI as proof of how fast the
graph engine answered - not how fast Python built a dict.

Handlers return plain dicts rather than pydantic response models. The contract
says type-specific node fields are *omitted* when not applicable
(``ecosystem`` on a Service, ``criticality`` on a Package), and a response model
would reinstate them as nulls. Request bodies *are* pydantic, which is what
produces the contract's 422s.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import analytics, closure_precision, remediation, schema
from .hydra_client import MAX_DEPTH, HydraClient, HydraQueryError, HydraUnavailableError
from .ingest_jobs import IngestJobStore, JobBusyError
from .pr_engine import PrEngineError, open_pr as run_open_pr

logger = logging.getLogger("radix.api")

#: The Vite dev server. Both spellings, because a browser sends whichever the
#: user typed and CORS matches the Origin header literally.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "RADIX_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]

DEFAULT_DEPTH = 6
DEFAULT_WINDOW_HOURS = 48


# --------------------------------------------------------------------------
# Request models - the source of the contract's 422s
# --------------------------------------------------------------------------


class SimulateBreachRequest(BaseModel):
    """``POST /api/simulate-breach``. Either ``package_id`` or ``package_name``."""

    package_id: int | None = Field(default=None, ge=0)
    package_name: str | None = Field(default=None, min_length=1, max_length=214)
    version: str | None = Field(default=None, max_length=64)
    window_hours: int = Field(default=DEFAULT_WINDOW_HOURS, ge=1, le=8760)
    depth: int = Field(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH)

    @model_validator(mode="after")
    def _require_a_target(self) -> "SimulateBreachRequest":
        if self.package_id is None and not self.package_name:
            raise ValueError("one of package_id or package_name is required")
        return self


class GenerateFixRequest(BaseModel):
    """``POST /api/generate-fix``. ``service_ids`` omitted patches everything exposed."""

    package_id: int | None = Field(default=None, ge=0)
    package_name: str | None = Field(default=None, min_length=1, max_length=214)
    bad_version: str | None = Field(default=None, max_length=64)
    service_ids: list[int] | None = Field(default=None, max_length=256)
    depth: int = Field(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH)

    @model_validator(mode="after")
    def _require_a_target(self) -> "GenerateFixRequest":
        if self.package_id is None and not self.package_name:
            raise ValueError("one of package_id or package_name is required")
        return self


# --------------------------------------------------------------------------
# Application wiring
# --------------------------------------------------------------------------

_client: HydraClient | None = None

#: Incidents are numbered sequentially per process, matching the contract's
#: ``RDX-2026-0001``. A real deployment would allocate these from a store.
_incident_counter = itertools.count(1)
_incident_lock = threading.Lock()


def _next_incident_id() -> str:
    with _incident_lock:
        sequence = next(_incident_counter)
    return f"RDX-{datetime.now(timezone.utc).year}-{sequence:04d}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One pooled HydraDB session for the process.

    Deliberately does *not* probe HydraDB at startup: the contract requires the
    dashboard to render a degraded state when the engine is down, which means
    the API has to boot without it.
    """
    global _client
    _client = HydraClient()
    logger.info("Radix API up; HydraDB target %s", _client.config.query_url)
    try:
        yield
    finally:
        _client.close()
        _client = None


app = FastAPI(
    title="Radix",
    version="1.0.0",
    summary="Graph-native software supply-chain security sentinel, powered by HydraDB.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_client() -> Iterator[HydraClient]:
    if _client is None:  # pragma: no cover - only reachable outside the lifespan
        raise HTTPException(status_code=503, detail="HydraDB client is not initialised")
    yield _client


@app.exception_handler(HydraUnavailableError)
async def _hydra_unavailable(request: Request, exc: HydraUnavailableError) -> JSONResponse:
    """The engine is unreachable - 503, not a stack trace."""
    logger.warning("HydraDB unavailable for %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "HydraDB is unavailable", "error": str(exc)})


@app.exception_handler(HydraQueryError)
async def _hydra_query_failed(request: Request, exc: HydraQueryError) -> JSONResponse:
    """The engine rejected or failed a statement - an upstream fault, so 502.

    The rejection message is surfaced verbatim: HydraDB names the exact
    unsupported construct, and hiding that would waste the best diagnostic
    the engine gives.
    """
    logger.error("HydraDB rejected a query for %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "HydraDB rejected the query", "code": exc.code, "error": exc.message},
    )


# --------------------------------------------------------------------------
# Shared handler helpers
# --------------------------------------------------------------------------


def _resolve_package(
    client: HydraClient, package_id: int | None, package_name: str | None
) -> dict[str, Any]:
    """Resolve a package by id or exact/prefix name, or raise 404."""
    if package_id is not None:
        node = client.get_node(int(package_id))
        if node is not None and node.get("label") == schema.PACKAGE:
            return node
        raise HTTPException(status_code=404, detail=f"no package with id {package_id}")

    node = client.resolve_package(str(package_name))
    if node is None:
        raise HTTPException(status_code=404, detail=f"no package named {package_name!r}")
    return node


def _closure_with_blast(
    client: HydraClient, package_id: int, depth: int
) -> tuple[dict[str, Any], analytics.Fleet]:
    """Reverse closure plus its blast radius, sharing one Fleet read."""
    closure = client.get_transitive_closure(package_id, depth)
    fleet = analytics.load_fleet(client)
    closure["blast_radius"] = analytics.compute_blast_radius(
        fleet,
        closure.get("affected_service_ids") or [],
        closure.get("affected_lockfile_ids") or [],
    )
    closure["latency_ms"] = round(float(closure.get("latency_ms") or 0.0) + fleet.latency_ms, 3)
    return closure, fleet


def _compromised_version(
    versions: list[dict[str, Any]],
    requested: str | None,
    package: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The version under investigation.

    Resolution order: the caller's explicit version; then the package's
    recorded MALICIOUS releases (`mal_versions`, written from MAL advisories at
    ingest time); then the earliest windowed release. The middle tier is what
    separates the compromise (debug 4.4.2) from an old vulnerability window
    (debug <=3.2.6) - the two produce opposite blast radii under version-exact
    pruning.
    """
    if requested:
        for version in versions:
            if str(version.get("semver")) == requested:
                return version
        # An unrecorded version is still a legitimate thing to simulate.
        return {"semver": requested, "compromised_window": True}

    mal_raw = str((package or {}).get("mal_versions") or "")
    mal = [v for v in mal_raw.split(",") if v]
    if mal:
        try:
            from . import semver_npm

            chosen = semver_npm.max_satisfying(mal, "*") or mal[-1]
        except Exception:  # pragma: no cover - ordering nicety only
            chosen = mal[-1]
        for version in versions:
            if str(version.get("semver")) == chosen:
                return version
        return {"semver": chosen, "compromised_window": True}

    windowed = [v for v in versions if v.get("compromised_window")]
    windowed.sort(key=lambda v: str(v.get("published_at") or ""))
    return windowed[0] if windowed else (versions[0] if versions else None)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
def health(client: HydraClient = Depends(get_client)) -> dict[str, Any]:
    """Liveness of the *query path*, not just the process.

    Never 500s: a dashboard that cannot reach HydraDB should render a degraded
    badge, so an unreachable engine is reported as ``hydra_ready: false``.
    """
    try:
        result = client.execute(f"MATCH (n:{schema.PACKAGE}) RETURN count(*) AS c", timeout_ms=2000)
    except (HydraUnavailableError, HydraQueryError) as exc:
        logger.warning("health check could not reach HydraDB: %s", exc)
        return {"status": "degraded", "hydra_ready": False, "latency_ms": 0.0, "seeded": False}

    packages = int(result.scalar("c", 0) or 0)
    return {
        "status": "ok",
        "hydra_ready": True,
        "latency_ms": round(result.latency_ms, 3),
        "seeded": packages > 0,
        "packages": packages,
    }


@app.get("/api/graph/full")
def graph_full(
    limit: int | None = Query(default=None, ge=1, le=100_000),
    client: HydraClient = Depends(get_client),
) -> dict[str, Any]:
    """The whole graph for the visualiser.

    Internal inverse edges (``DEPENDED_ON_BY``, ``MAINTAINS``) are excluded -
    they exist only to satisfy the forward-traversal rule and would draw every
    dependency twice.
    """
    return client.get_full_graph(limit)


@app.get("/api/closure/{package_id}")
def closure(
    package_id: int = Path(ge=0),
    depth: int = Query(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH),
    client: HydraClient = Depends(get_client),
) -> dict[str, Any]:
    """Reverse transitive closure: everything that depends on ``package_id``.

    Walks the materialised ``DEPENDED_ON_BY`` edge, because HydraDB rejects
    target-anchored variable-length patterns and this question *is* that shape.
    """
    if client.get_node(package_id) is None:
        raise HTTPException(status_code=404, detail=f"no node with id {package_id}")
    result, _fleet = _closure_with_blast(client, package_id, depth)

    # Version-exact refinement: the traversal is package-level; prune it with
    # the semver evidence. The bad version is the earliest windowed release,
    # same convention as remediation.
    versions, elapsed = remediation.versions_for_package(client, package_id)
    result["latency_ms"] = round(float(result["latency_ms"]) + elapsed, 3)
    package = client.get_node(package_id) or {}
    chosen = _compromised_version(versions, None, package)
    bad_version = str(chosen.get("semver")) if chosen and chosen.get("compromised_window") else None
    return closure_precision.refine(client, result, bad_version)


@app.post("/api/simulate-breach")
def simulate_breach(
    request: SimulateBreachRequest, client: HydraClient = Depends(get_client)
) -> dict[str, Any]:
    """The headline call: one package, one version, the whole incident report."""
    package = _resolve_package(client, request.package_id, request.package_name)
    package_id = int(package["id"])

    closure_result, fleet = _closure_with_blast(client, package_id, request.depth)
    latency_ms = float(closure_result["latency_ms"])

    versions, elapsed = remediation.versions_for_package(client, package_id)
    latency_ms += elapsed
    compromised_version = _compromised_version(versions, request.version, package)

    # Version-exact refinement must run before anything reads the blast
    # radius: the timeline, risk profile and headline numbers all describe the
    # pruned closure, not the package-level over-approximation.
    closure_result = closure_precision.refine(
        client, closure_result, (compromised_version or {}).get("semver")
    )
    blast = closure_result["blast_radius"]

    maintainer_id, elapsed = analytics.primary_maintainer_id(client, package_id)
    latency_ms += elapsed
    maintainer_report = None
    if maintainer_id is not None:
        maintainer_report = analytics.maintainer_risk(
            client,
            maintainer_id,
            window_hours=request.window_hours,
            exclude_package_id=package_id,
        )
        if maintainer_report:
            latency_ms += float(maintainer_report["latency_ms"])

    typosquat_report = analytics.typosquats(client, package_id) or {"candidates": []}
    latency_ms += float(typosquat_report.get("latency_ms") or 0.0)
    candidates = typosquat_report.get("candidates") or []

    safe_version, _reason = remediation.select_safe_version(
        versions, str((compromised_version or {}).get("semver") or "")
    )

    timeline = analytics.build_timeline(
        package=package,
        compromised_version=compromised_version,
        safe_version=safe_version,
        versions=versions,
        maintainer_report=maintainer_report,
        typosquat_candidates=candidates,
        blast=blast,
        fleet=fleet,
        exposed_service_ids=closure_result.get("affected_service_ids") or [],
        affected_package_count=len(closure_result.get("affected_package_ids") or []),
        window_hours=request.window_hours,
    )

    risk = analytics.package_risk_profile(
        closure=closure_result,
        maintainer_risk_report=maintainer_report,
        typosquat_report=typosquat_report,
        in_compromised_window=bool((compromised_version or {}).get("compromised_window")),
    )

    return {
        "incident_id": _next_incident_id(),
        "root": package,
        "compromised_version": (compromised_version or {}).get("semver"),
        "window_hours": request.window_hours,
        "closure": closure_result,
        "maintainer_risk": maintainer_report,
        "typosquats": candidates,
        "blast_radius": blast,
        "timeline": timeline,
        "risk": risk,
        "safe_version": safe_version.get("semver") if safe_version else None,
        "latency_ms": round(latency_ms, 3),
    }


@app.get("/api/maintainer-risk/{maintainer_id}")
def maintainer_risk(
    maintainer_id: int = Path(ge=0),
    window_hours: int = Query(default=DEFAULT_WINDOW_HOURS, ge=1, le=8760),
    client: HydraClient = Depends(get_client),
) -> dict[str, Any]:
    """Two-hop maintainer subgraph: which of their other packages also shipped."""
    report = analytics.maintainer_risk(client, maintainer_id, window_hours=window_hours)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no maintainer with id {maintainer_id}")
    return report


@app.get("/api/typosquats/{package_id}")
def typosquats(
    package_id: int = Path(ge=0), client: HydraClient = Depends(get_client)
) -> dict[str, Any]:
    """Impersonating packages, ranked closest-first.

    Edit distance and similarity are computed at seed time - HydraDB has no
    string functions - and stored on the ``TYPOSQUAT_OF`` edge.
    """
    report = analytics.typosquats(client, package_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no package with id {package_id}")
    return report


@app.post("/api/generate-fix")
def generate_fix(
    request: GenerateFixRequest, client: HydraClient = Depends(get_client)
) -> dict[str, Any]:
    """Pick the safe version, render the lockfile diffs, write the PR."""
    package = _resolve_package(client, request.package_id, request.package_name)
    package_id = int(package["id"])

    closure_result, fleet = _closure_with_blast(client, package_id, request.depth)
    exposed_service_ids = closure_result.get("affected_service_ids") or []
    latency_ms = float(closure_result["latency_ms"])

    maintainer_note = None
    maintainer_id, elapsed = analytics.primary_maintainer_id(client, package_id)
    latency_ms += elapsed
    if maintainer_id is not None:
        report = analytics.maintainer_risk(
            client, maintainer_id, exclude_package_id=package_id
        )
        if report:
            latency_ms += float(report["latency_ms"])
            handle = report["maintainer"].get("username") or report["maintainer"].get("name")
            maintainer_note = f"`{handle}` - {report['risk_note']}"

    typosquat_report = analytics.typosquats(client, package_id) or {"candidates": []}
    latency_ms += float(typosquat_report.get("latency_ms") or 0.0)

    fix = remediation.generate_fix(
        client,
        package=package,
        bad_version=request.bad_version,
        service_ids=request.service_ids,
        exposed_service_ids=exposed_service_ids,
        fleet=fleet,
        blast=closure_result["blast_radius"],
        maintainer_note=maintainer_note,
        typosquat_count=len(typosquat_report.get("candidates") or []),
    )
    # The handler's own traversals count too: latency_ms is the whole request's
    # HydraDB cost, not just the remediation module's share of it.
    fix["latency_ms"] = round(latency_ms + float(fix.get("latency_ms") or 0.0), 3)

    if fix.get("safe_version") is None and not fix.get("patches"):
        # A package with no safe predecessor is a real answer, not an error -
        # the reason string explains it and the UI shows it as such.
        logger.info("no safe rollback target for package %s: %s", package_id, fix.get("reason"))
    return fix


# ---------------------------------------------------------------------------
# POST /api/open-pr - a real remediation branch, not a rendered preview
# ---------------------------------------------------------------------------


class OpenPrRequest(BaseModel):
    """``POST /api/open-pr``. The service names the repository receiving the branch."""

    package_id: int = Field(ge=0)
    service_id: int = Field(ge=0)
    bad_version: str | None = Field(default=None, max_length=64)
    safe_version: str | None = Field(default=None, max_length=64)
    dry_run: bool = True


@app.post("/api/open-pr")
def open_pr_route(
    request: OpenPrRequest, client: HydraClient = Depends(get_client)
) -> dict[str, Any]:
    package = client.get_node(request.package_id)
    if package is None or package.get("label") != schema.PACKAGE:
        raise HTTPException(status_code=404, detail=f"no package with id {request.package_id}")
    service = client.get_node(request.service_id)
    if service is None or service.get("label") != schema.SERVICE:
        raise HTTPException(status_code=404, detail=f"no service with id {request.service_id}")

    repo_target = str(service.get("repo_url") or "").strip()
    if not repo_target:
        raise HTTPException(
            status_code=422,
            detail=f"service {service.get('name')!r} has no repository on record; "
            "re-ingest it from a git URL or local path",
        )
    # The registry stores host/org/repo without a scheme; git needs one.
    if repo_target.startswith("github.com/"):
        repo_target = f"https://{repo_target}"

    safe_version = request.safe_version
    bad_version = request.bad_version
    if safe_version is None:
        versions, _ = remediation.versions_for_package(client, int(package["id"]))
        if bad_version is None:
            windowed = sorted(
                (v for v in versions if v.get("compromised_window")),
                key=lambda v: str(v.get("published_at") or ""),
            )
            bad_version = str(windowed[0].get("semver")) if windowed else None
        safe, reason = remediation.select_safe_version(versions, str(bad_version or ""))
        if not safe:
            raise HTTPException(status_code=422, detail=f"no safe version to pin: {reason}")
        safe_version = str(safe.get("semver"))

    token = os.environ.get("GITHUB_TOKEN") or None
    if not request.dry_run and not token:
        raise HTTPException(
            status_code=400,
            detail="pushing requires GITHUB_TOKEN on the backend; "
            "re-run with dry_run=true or configure the token",
        )

    try:
        return run_open_pr(
            repo_target=repo_target,
            package_name=str(package.get("name")),
            safe_version=safe_version,
            bad_version=bad_version,
            service_name=str(service.get("name")),
            dry_run=request.dry_run,
            github_token=token,
        )
    except PrEngineError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


# ---------------------------------------------------------------------------
# POST /api/ingest + GET /api/ingest/{job_id} - repo submission from the UI
# ---------------------------------------------------------------------------


class IngestStartRequest(BaseModel):
    target: str = Field(min_length=1, max_length=500)


_ingest_jobs = IngestJobStore()


@app.post("/api/ingest", status_code=202)
def ingest_start(
    request: IngestStartRequest, client: HydraClient = Depends(get_client)
) -> dict[str, Any]:
    """Ingest a repository into the namespace this backend serves.

    The demo namespace is read-only by design, so against the default dev
    backend this returns 409 with instructions rather than corrupting the
    seeded world - run ``make live-backend`` for the real-data console.
    """
    try:
        job_id = _ingest_jobs.start(request.target, client.config.namespace)
    except JobBusyError as busy:
        raise HTTPException(status_code=409, detail=str(busy)) from busy
    except ValueError as invalid:
        raise HTTPException(status_code=409, detail=str(invalid)) from invalid
    return {"job_id": job_id}


@app.get("/api/ingest/{job_id}")
def ingest_status(job_id: str) -> dict[str, Any]:
    job = _ingest_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown ingest job {job_id!r}")
    return job
