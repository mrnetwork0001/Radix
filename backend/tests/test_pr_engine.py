"""Unit tests for app.pr_engine - overrides merge, token hygiene, dry-run flow.

Hermetic: the npm regeneration step is monkeypatched (the live-registry path is
exercised by the manual verification run against examples/legacy-billing), and
every git operation runs inside pytest's tmp_path.

Runnable two ways:
  * pytest backend/tests/test_pr_engine.py
  * backend/.venv/bin/python -m pytest backend/tests/test_pr_engine.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pr_engine  # noqa: E402
from app.pr_engine import PrEngineError, merge_overrides, open_pr  # noqa: E402

FAKE_TOKEN = "ghp_FAKEtoken1234567890SECRETSECRET"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    """A tiny committed git repo declaring lodash 4.17.19, plus existing overrides."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-billing",
                "version": "1.0.0",
                "private": True,
                "dependencies": {"lodash": "4.17.19", "left-pad": "1.3.0"},
                "overrides": {"minimist": "1.2.8"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(["init", "-b", "main"], repo)
    _git(["-c", "user.name=Fixture", "-c", "user.email=f@localhost", "add", "-A"], repo)
    _git(
        ["-c", "user.name=Fixture", "-c", "user.email=f@localhost", "commit", "-m", "init"],
        repo,
    )
    return repo


@pytest.fixture()
def no_npm(monkeypatch: pytest.MonkeyPatch):
    """Skip the live-registry step so tests stay hermetic and fast."""
    monkeypatch.setattr(
        pr_engine,
        "_regenerate_lockfile",
        lambda manifest_dir: (False, "npm skipped in unit tests"),
    )


def _snapshot(repo: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(repo)): p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------
# merge_overrides
# ---------------------------------------------------------------------------


def test_merge_overrides_preserves_existing_entries():
    manifest = {
        "name": "x",
        "dependencies": {"lodash": "^4.17.0"},
        "overrides": {"minimist": "1.2.8", "qs": {".": "6.11.0"}},
    }
    merged = merge_overrides(manifest, "lodash", "4.17.21")
    assert merged["overrides"] == {
        "minimist": "1.2.8",
        "qs": {".": "6.11.0"},
        "lodash": "4.17.21",
    }
    # Pure: the input manifest is untouched.
    assert manifest["overrides"] == {"minimist": "1.2.8", "qs": {".": "6.11.0"}}
    # Non-overrides fields carried through unchanged.
    assert merged["dependencies"] == {"lodash": "^4.17.0"}


def test_merge_overrides_creates_block_when_absent():
    merged = merge_overrides({"name": "x"}, "lodash", "4.17.21")
    assert merged["overrides"] == {"lodash": "4.17.21"}


def test_merge_overrides_replaces_stale_pin_for_same_package():
    merged = merge_overrides({"overrides": {"lodash": "4.17.20"}}, "lodash", "4.17.21")
    assert merged["overrides"] == {"lodash": "4.17.21"}


# ---------------------------------------------------------------------------
# Dry-run pipeline
# ---------------------------------------------------------------------------


def test_dry_run_without_token_succeeds(source_repo: Path, no_npm, tmp_path: Path):
    before = _snapshot(source_repo)
    result = open_pr(
        repo_target=str(source_repo),
        package_name="lodash",
        safe_version="4.17.21",
        bad_version="4.17.19",
        service_name="fixture-billing",
        dry_run=True,
        workdir=tmp_path / "scratch-parent",
    )

    assert result["mode"] == "dry-run"
    assert result["branch"] == "radix/pin-lodash-4.17.21"
    assert result["base"] == "main"
    assert result["regenerated"] is False
    assert "pr_url" not in result
    # The merge preserved the pre-existing pin and added the new one.
    assert result["overrides"] == {"minimist": "1.2.8", "lodash": "4.17.21"}
    # The diff is a real git commit: hash header plus the manifest hunk.
    assert result["diff"].startswith("commit ")
    assert 'fix(security): pin lodash to 4.17.21' in result["diff"]
    assert '+    "lodash": "4.17.21"' in result["diff"]
    assert "GITHUB_TOKEN" in result["message"]
    # The source checkout was never written to.
    assert _snapshot(source_repo) == before
    # The scratch clone was cleaned up after capture.
    parent = tmp_path / "scratch-parent"
    assert not any(parent.iterdir())


def test_dry_run_commit_is_attributed_to_radix_not_the_user(source_repo: Path, no_npm):
    result = open_pr(
        repo_target=str(source_repo),
        package_name="lodash",
        safe_version="4.17.21",
        bad_version=None,
        service_name="fixture-billing",
    )
    assert "Author: Radix Sentinel <radix@localhost>" in result["diff"]


def test_token_never_appears_in_returned_or_logged_strings(
    source_repo: Path, no_npm, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG, logger="radix.pr_engine")
    result = open_pr(
        repo_target=str(source_repo),
        package_name="lodash",
        safe_version="4.17.21",
        bad_version="4.17.19",
        service_name="fixture-billing",
        dry_run=True,
        github_token=FAKE_TOKEN,
    )
    assert FAKE_TOKEN not in json.dumps(result)
    assert FAKE_TOKEN not in caplog.text


def test_token_never_appears_in_raised_errors(source_repo: Path):
    # Local path + dry_run=False is rejected; the error must not echo the token.
    with pytest.raises(PrEngineError) as excinfo:
        open_pr(
            repo_target=str(source_repo),
            package_name="lodash",
            safe_version="4.17.21",
            bad_version=None,
            service_name="fixture-billing",
            dry_run=False,
            github_token=FAKE_TOKEN,
        )
    assert FAKE_TOKEN not in str(excinfo.value)
    assert "github.com" in str(excinfo.value)


def test_dry_run_false_without_token_raises_clear_error(source_repo: Path):
    with pytest.raises(PrEngineError, match="GitHub token"):
        open_pr(
            repo_target=str(source_repo),
            package_name="lodash",
            safe_version="4.17.21",
            bad_version=None,
            service_name="fixture-billing",
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_non_repo_local_path_is_rejected(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PrEngineError, match="not a git repository"):
        open_pr(
            repo_target=str(plain),
            package_name="lodash",
            safe_version="4.17.21",
            bad_version=None,
            service_name="svc",
        )


def test_undeclared_package_is_rejected(source_repo: Path, no_npm):
    with pytest.raises(PrEngineError, match="no package.json declaring"):
        open_pr(
            repo_target=str(source_repo),
            package_name="totally-absent-package",
            safe_version="1.0.0",
            bad_version=None,
            service_name="svc",
        )


def test_branch_slug_handles_scoped_packages():
    assert pr_engine._slug("@scope/pkg") == "scope-pkg"
    assert pr_engine._slug("lodash") == "lodash"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
