"""Unit tests for :mod:`app.ingest_jobs` with a fully faked pipeline.

No network, no HydraDB: ``scan_source`` and the writer/registry/OSV factories
are injected, so these tests exercise the job lifecycle itself - log
progression, the frozen report shape, single-flight busy rejection, error
capture, target validation and finished-job pruning.

Runs standalone (``backend/.venv/bin/python backend/tests/test_ingest_jobs.py``)
or under pytest.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest_jobs import IngestJobStore, JobBusyError
from app.ingest.model import (
    Advisory,
    DepEdge,
    IngestReport,
    PackageRelease,
    ParsedLockfile,
    RepoScan,
)

WAIT_S = 5.0

#: Every key the frozen IngestJob.report shape requires (types.ts).
REPORT_KEYS = {
    "packages",
    "versions",
    "maintainers",
    "services",
    "lockfiles",
    "depends_on",
    "typosquats",
    "compromised_marked",
    "advisories",
    "malicious",
}


# --- fakes ------------------------------------------------------------------


def fake_scan(target: str) -> RepoScan:
    lockfile = ParsedLockfile(
        path="package-lock.json",
        kind="npm",
        root_name="demo",
        releases=[PackageRelease(name="left-pad", version="1.3.0")],
        edges=[
            DepEdge(
                src_name=None,
                src_version=None,
                dst_name="left-pad",
                constraint="^1.0.0",
                dst_version="1.3.0",
            )
        ],
    )
    return RepoScan(
        source=target,
        repo_name="demo",
        repo_url=None,
        commit_hash="abc123",
        lockfiles=[lockfile],
    )


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ingest(self, scan, meta, advisories) -> IngestReport:
        self.calls.append((scan, meta, advisories))
        return IngestReport(
            namespace="radix/test",
            services=1,
            lockfiles=1,
            packages=1,
            versions=1,
            maintainers=0,
            depends_on=1,
            resolved_in=2,
            maintained_by=0,
            typosquats=0,
            compromised_marked=1,
            statements=5,
            wire_seconds=0.01,
        )


class FakeOsv:
    def advisories_for(self, packages):
        return [
            Advisory(id="MAL-2024-0001", package="left-pad", malicious=True),
            Advisory(id="GHSA-xxxx-yyyy-zzzz", package="left-pad"),
        ]


def wait_until_finished(store: IngestJobStore, job_id: str) -> dict:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        job = store.get(job_id)
        assert job is not None
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} still running after {WAIT_S}s")


def make_store(**overrides) -> IngestJobStore:
    defaults = dict(
        scan_source=fake_scan,
        writer_factory=lambda namespace: FakeWriter(),
        registry_factory=lambda: None,
        osv_factory=lambda: FakeOsv(),
    )
    defaults.update(overrides)
    return IngestJobStore(**defaults)


# --- happy path -------------------------------------------------------------


def test_happy_path_log_progression_and_report(tmp_path):
    store = make_store()
    job_id = store.start(str(tmp_path), "radix/test")

    job = wait_until_finished(store, job_id)
    assert job["status"] == "done"
    assert job["job_id"] == job_id
    assert job["target"] == str(tmp_path)
    assert "error" not in job

    log = job["log"]
    assert log[0] == f"── scanning {tmp_path}"
    assert log[1] == "   package-lock.json: 1 releases, 1 edges (npm)"
    assert "   registry: enrichment skipped" in log
    assert "   osv: querying 1 packages…" in log
    assert "   osv: 2 advisories (1 malicious)" in log
    wrote = [line for line in log if line.startswith("   wrote:")]
    assert wrote == [
        "   wrote: 1 packages, 1 versions, 0 maintainers, 1 services, "
        "1 lockfiles | 1 DEPENDS_ON, 2 RESOLVED_IN, 0 typosquats | "
        "1 compromised | 5 statements, 0.01s on the wire"
    ]
    assert log[-1].startswith("── done in ")

    report = job["report"]
    assert set(report) == REPORT_KEYS
    assert report["packages"] == 1
    assert report["versions"] == 1
    assert report["services"] == 1
    assert report["lockfiles"] == 1
    assert report["depends_on"] == 1
    assert report["compromised_marked"] == 1
    assert report["advisories"] == 2
    assert report["malicious"] == 1


def test_no_lockfiles_is_done_with_empty_report(tmp_path):
    def empty_scan(target: str) -> RepoScan:
        return RepoScan(
            source=target, repo_name="empty", repo_url=None, commit_hash=None, lockfiles=[]
        )

    store = make_store(scan_source=empty_scan)
    job_id = store.start(str(tmp_path), "radix/test")
    job = wait_until_finished(store, job_id)

    assert job["status"] == "done"
    assert "   no lockfiles found - nothing to ingest" in job["log"]
    assert set(job["report"]) == REPORT_KEYS
    assert all(value == 0 for value in job["report"].values())


# --- busy rejection ---------------------------------------------------------


def test_second_start_while_running_raises_busy(tmp_path):
    release = threading.Event()
    entered = threading.Event()

    def blocking_scan(target: str) -> RepoScan:
        entered.set()
        assert release.wait(WAIT_S), "test never released the worker"
        return fake_scan(target)

    store = make_store(scan_source=blocking_scan)
    first = store.start(str(tmp_path), "radix/test")
    assert entered.wait(WAIT_S)

    with pytest.raises(JobBusyError) as excinfo:
        store.start(str(tmp_path), "radix/test")
    assert first in str(excinfo.value)  # the active job id is in the message

    release.set()
    assert wait_until_finished(store, first)["status"] == "done"

    # The slot frees up once the job finishes.
    second = store.start(str(tmp_path), "radix/test")
    assert second != first
    assert wait_until_finished(store, second)["status"] == "done"


# --- error capture ----------------------------------------------------------


def test_pipeline_error_is_captured_not_raised(tmp_path):
    calls = []

    def exploding_scan(target: str) -> RepoScan:
        if not calls:  # fail the first job only, so the retry can succeed
            calls.append(target)
            raise RuntimeError("clone failed: repository not found")
        return fake_scan(target)

    store = make_store(scan_source=exploding_scan)
    job_id = store.start(str(tmp_path), "radix/test")
    job = wait_until_finished(store, job_id)

    assert job["status"] == "error"
    assert job["error"] == "clone failed: repository not found"
    assert "report" not in job
    assert any("error: clone failed" in line for line in job["log"])

    # An errored job releases the single-flight slot.
    next_id = store.start(str(tmp_path), "radix/test")
    assert wait_until_finished(store, next_id)["status"] == "done"


# --- validation -------------------------------------------------------------


def test_start_rejects_bad_targets_and_namespaces(tmp_path):
    store = make_store()

    with pytest.raises(ValueError):
        store.start("", "radix/test")
    with pytest.raises(ValueError):
        store.start("   ", "radix/test")
    with pytest.raises(ValueError):
        store.start(str(tmp_path / "does-not-exist"), "radix/test")
    with pytest.raises(ValueError):
        store.start(str(tmp_path), "radix")  # the demo namespace is read-only
    with pytest.raises(ValueError):
        store.start(str(tmp_path), "")

    # Git URLs (https and ssh forms) pass validation without touching disk.
    for url in ("https://github.com/org/repo", "git@github.com:org/repo.git"):
        job = wait_until_finished(store, store.start(url, "radix/test"))
        assert job["status"] == "done"
        assert job["target"] == url


# --- retention --------------------------------------------------------------


def test_finished_jobs_are_pruned_to_the_cap(tmp_path):
    store = make_store(max_finished=3)
    ids = []
    for _ in range(5):
        job_id = store.start(str(tmp_path), "radix/test")
        wait_until_finished(store, job_id)
        ids.append(job_id)

    assert store.get(ids[0]) is None
    assert store.get(ids[1]) is None
    for kept in ids[2:]:
        job = store.get(kept)
        assert job is not None and job["status"] == "done"

    assert store.get("nonexistent") is None


# --- snapshot isolation -----------------------------------------------------


def test_get_returns_a_snapshot_not_live_state(tmp_path):
    store = make_store()
    job_id = store.start(str(tmp_path), "radix/test")
    job = wait_until_finished(store, job_id)

    job["log"].append("tampered")
    job["report"]["packages"] = 999
    job["status"] = "error"

    fresh = store.get(job_id)
    assert fresh is not None
    assert "tampered" not in fresh["log"]
    assert fresh["report"]["packages"] == 1
    assert fresh["status"] == "done"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
