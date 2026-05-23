"""Tests for ISSUE_ORCHESTRATOR_PYTHON resolution in the bundled pre-push hook.

The orchestrator installs a pre-push hook into every target worktree. That
hook runs a Python helper from the ``issue_orchestrator`` package, but the
target repo (Kotlin/Node/etc.) typically has no venv where the package is
importable. This module tests the end-to-end contract: the orchestrator
exports ``ISSUE_ORCHESTRATOR_PYTHON`` at bootstrap, and the rendered hook
prefers that path over its legacy lookups.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.bootstrap import (
    ISSUE_ORCHESTRATOR_PYTHON_ENV,
    export_orchestrator_python,
)


_HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "issue_orchestrator"
    / "hooks"
    / "pre-push"
)
_HOOK_FRAGMENT_TIMEOUT_SECONDS = 30


def _make_fake_python(tmp_path: Path, name: str, marker_line: str) -> Path:
    """Create an executable shell script masquerading as python.

    Takes whatever arguments and prints ``marker_line`` to stdout, exits 0.
    Used so the test can see which interpreter the hook actually invokes
    without importing the real ``issue_orchestrator`` module.
    """
    fake = tmp_path / name
    fake.write_text(
        "#!/bin/bash\n"
        f"echo {marker_line!r}\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _run_hook_fragment(
    *,
    worktree: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Execute only the python-resolution + invoke part of the bundled hook.

    The full hook does more (test-skipping guard, etc.) that we don't want
    to exercise here. We take everything through the third ``fi`` in the
    script, which is a structural boundary: the first two close the
    Python-resolution if/elif chain and the empty-check, the third closes
    the dirty-tree guard invocation. Using a structural marker means
    changes to log strings don't break the slice; changes to the *shape*
    of the hook (which SHOULD invalidate the test) still do.
    """
    script = _HOOK_PATH.read_text().splitlines()
    fi_indices = [
        idx for idx, line in enumerate(script) if line.strip() == "fi"
    ]
    if len(fi_indices) < 3:
        raise AssertionError(
            "bundled pre-push hook no longer has the expected three ``fi`` "
            "closings — update this slice or the hook; don't just keep "
            "scrolling."
        )
    end = fi_indices[2] + 1  # slice-end exclusive; include the third fi
    fragment = "\n".join(script[:end]) + "\n"
    return subprocess.run(
        ["/bin/bash", "-c", fragment],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        # Keep the hook invocation bounded, but allow loaded xdist workers enough
        # scheduler time to spawn bash and the fake interpreter.
        timeout=_HOOK_FRAGMENT_TIMEOUT_SECONDS,
    )


def test_hook_prefers_issue_orchestrator_python(tmp_path: Path) -> None:
    """When the env var points at a real executable, the hook uses it."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    fake = _make_fake_python(tmp_path, "orchestrator-python", "USED_ENV_VAR")

    result = _run_hook_fragment(
        worktree=worktree,
        env={
            **os.environ,
            "ISSUE_ORCHESTRATOR_PYTHON": str(fake),
            "PATH": "/usr/bin:/bin",  # no python3 on PATH to force the env var
        },
    )

    assert result.returncode == 0, result.stderr
    assert "USED_ENV_VAR" in result.stdout


def test_hook_falls_back_to_worktree_venv_when_env_unset(tmp_path: Path) -> None:
    """Legacy behaviour preserved: .venv/bin/python wins without the env var."""
    worktree = tmp_path / "repo"
    venv_bin = worktree / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = _make_fake_python(venv_bin, "python", "USED_VENV_PYTHON")

    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
    }
    env.pop("ISSUE_ORCHESTRATOR_PYTHON", None)

    result = _run_hook_fragment(worktree=worktree, env=env)

    assert result.returncode == 0, result.stderr
    assert "USED_VENV_PYTHON" in result.stdout


def test_hook_falls_back_to_path_python3_when_nothing_else(tmp_path: Path) -> None:
    """When neither env var nor local venv is available, PATH's python3 wins."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    fake_dir = tmp_path / "ambient-bin"
    fake_dir.mkdir()
    _make_fake_python(fake_dir, "python3", "USED_PATH_PYTHON3")

    env = {
        **os.environ,
        "PATH": f"{fake_dir}:/usr/bin:/bin",
    }
    env.pop("ISSUE_ORCHESTRATOR_PYTHON", None)

    result = _run_hook_fragment(worktree=worktree, env=env)

    assert result.returncode == 0, result.stderr
    assert "USED_PATH_PYTHON3" in result.stdout


def test_hook_ignores_env_var_pointing_at_missing_file(tmp_path: Path) -> None:
    """A stale env var path must fall through to ambient lookup, not fail loud."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    fake_dir = tmp_path / "ambient-bin"
    fake_dir.mkdir()
    _make_fake_python(fake_dir, "python3", "USED_FALLBACK")

    result = _run_hook_fragment(
        worktree=worktree,
        env={
            **os.environ,
            "ISSUE_ORCHESTRATOR_PYTHON": str(tmp_path / "does-not-exist"),
            "PATH": f"{fake_dir}:/usr/bin:/bin",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "USED_FALLBACK" in result.stdout


def test_hook_fails_cleanly_when_no_python_found(tmp_path: Path) -> None:
    """With no resolvable python anywhere, exit 1 with actionable hints for both audiences."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    env = {
        "PATH": "/nonexistent",
        "HOME": str(tmp_path),
    }

    result = _run_hook_fragment(worktree=worktree, env=env)

    assert result.returncode == 1
    assert "Python not found" in result.stderr
    # Both audiences are named so operator + manual user each know what to do.
    assert "ISSUE_ORCHESTRATOR_PYTHON" in result.stderr
    assert ".venv" in result.stderr


def test_export_orchestrator_python_sets_env_to_sys_executable(monkeypatch) -> None:
    """The bootstrap helper exports sys.executable when no override exists."""
    monkeypatch.delenv(ISSUE_ORCHESTRATOR_PYTHON_ENV, raising=False)

    export_orchestrator_python()

    assert os.environ[ISSUE_ORCHESTRATOR_PYTHON_ENV] == sys.executable


def test_export_orchestrator_python_respects_existing_override(monkeypatch) -> None:
    """setdefault must not clobber an operator's explicit override."""
    monkeypatch.setenv(ISSUE_ORCHESTRATOR_PYTHON_ENV, "/custom/path/python")

    export_orchestrator_python()

    assert os.environ[ISSUE_ORCHESTRATOR_PYTHON_ENV] == "/custom/path/python"


def test_build_orchestrator_wires_export() -> None:
    """Regression pin: deleting the call from build_orchestrator breaks this.

    A pure behavioural test would require a live GitHub config to run the
    full composition root, so we inspect the source instead. If the helper
    is ever renamed, the constant at the top of this module keeps the
    assertion honest.
    """
    bootstrap_src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "issue_orchestrator"
        / "entrypoints"
        / "bootstrap.py"
    ).read_text()

    # Grab just the body of build_orchestrator so an ``export_orchestrator_python``
    # call anywhere else in the module (a future helper, for example) doesn't
    # satisfy this check by accident.
    body = bootstrap_src.split("def build_orchestrator(", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert "export_orchestrator_python()" in body, (
        "build_orchestrator no longer calls export_orchestrator_python; "
        "pre-push hooks in target repos will fail to import issue_orchestrator."
    )
