"""Canonical filesystem paths for E2E run artifacts."""

from __future__ import annotations

from pathlib import Path


def e2e_results_dir(repo_root: Path) -> Path:
    """Return the root directory for E2E result artifacts."""
    return repo_root / ".issue-orchestrator" / "e2e-results"


def run_report_artifact_dir(repo_root: Path, run_id: int) -> Path:
    """Return the worktree-local, run-scoped report artifact directory."""
    return e2e_results_dir(repo_root) / f"run_{run_id}"


def runtime_output_dir(repo_root: Path, run_id: int) -> Path:
    """Return the run-scoped runtime output directory."""
    return run_report_artifact_dir(repo_root, run_id) / "runtime-output"
