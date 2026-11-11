"""The area-test job skips paths that have not landed and fails when a suite fails."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "run-area-tests.sh"

PYTEST_DIRS = ("pkg/agent/tests", "central/tests", "platform/tests", "tests")
SELFHOST = "pkg/host/check-selfhost.sh"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_and_workflow_name_every_area_path() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    for path in (*PYTEST_DIRS, SELFHOST):
        assert path in script
    assert "bash .github/scripts/run-area-tests.sh" in workflow
    assert "pytest==9.2.0" in workflow
    assert "continue-on-error" not in workflow


def test_missing_paths_are_skipped(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    for path in (*PYTEST_DIRS, SELFHOST):
        assert f"skip: {path} does not exist yet" in result.stdout
    assert "running pytest" not in result.stdout
    assert "running pkg/host/check-selfhost.sh" not in result.stdout


def test_empty_pytest_dir_does_not_fail_the_job(tmp_path: Path) -> None:
    directory = tmp_path / "pkg" / "agent" / "tests"
    directory.mkdir(parents=True)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "running pytest in pkg/agent/tests" in result.stdout
    assert "skip: pkg/agent/tests exists but pytest collected no tests" in result.stdout
    assert "skip: central/tests does not exist yet" in result.stdout


def test_failing_pytest_fails_the_job(tmp_path: Path) -> None:
    directory = tmp_path / "platform" / "tests"
    directory.mkdir(parents=True)
    (directory / "test_fail.py").write_text(
        "def test_area_suite_failed():\n    assert False\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "running pytest in platform/tests" in result.stdout
    assert "pytest failed in platform/tests" in result.stderr


def test_selfhost_check_runs_when_present(tmp_path: Path) -> None:
    script = tmp_path / "pkg" / "host" / "check-selfhost.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho selfhost-ok\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "running pkg/host/check-selfhost.sh" in result.stdout
    assert "selfhost-ok" in result.stdout


def test_failing_selfhost_check_fails_the_job(tmp_path: Path) -> None:
    script = tmp_path / "pkg" / "host" / "check-selfhost.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho selfhost-bad >&2\nexit 1\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "running pkg/host/check-selfhost.sh" in result.stdout
