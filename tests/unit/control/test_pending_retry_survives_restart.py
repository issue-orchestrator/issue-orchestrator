"""A queued retry's checkout survives an orchestrator restart (#7273).

A retry's session is not active -- that is what makes it a retry -- so startup
reconciliation saw an inactive scratch checkout and removed it WITH ITS BRANCH.
For a tech-lead investigation that branch is never pushed, so the commits on it
exist nowhere else: the restart was the thing that destroyed the work the retry
was queued to resume.

Two halves have to hold for that to stop happening, and both are exercised here
against a real git worktree: startup recovery has to FIND the retry in a
checkout the numeric-branch scan never reaches, and reconciliation then has to
read the re-queued retry as evidence that the checkout is claimed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.control.validation_retry_recovery import (
    ValidationRetryRecovery,
)
from issue_orchestrator.control.worktree_reconciliation import (
    StartupWorktreeReconciler,
    WorktreeAuditOwner,
)
from issue_orchestrator.domain.models import OrchestratorState, PendingValidationRetry
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.session_run import SessionRunIdentity
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.ports.worktree_manager import WORKTREE_ID_MARKER

TOKEN = "abcdef123456"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--initial-branch=main", ".")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    return root


@pytest.fixture
def investigation(repo: Path, tmp_path: Path) -> Path:
    """A disposable investigation checkout with a commit only it has."""
    base = tmp_path / "worktree"
    base.mkdir()
    path = base / f"{repo.name}-tech-lead-6410-{TOKEN}"
    _git(repo, "worktree", "add", "-b", f"tech-lead-investigation-6410-{TOKEN}", str(path))
    marker = path / WORKTREE_ID_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("wt-test\n")
    (path / "finding.md").write_text("the only copy of this work\n")
    _git(path, "add", "finding.md")
    _git(path, "commit", "-m", "the finding")
    return path


def _reconciler(repo: Path, base: Path, config_cls) -> StartupWorktreeReconciler:
    manager = GitWorktreeManager()
    config = config_cls(repo_root=repo, worktree_base=base)
    return StartupWorktreeReconciler(
        config, None, manager, WorktreeAuditOwner(manager), None
    )


def _retry(checkout: Path) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=6410,
        issue_title="Investigate stranded failure",
        agent_label="agent:tech-lead",
        worktree_path=str(checkout),
        branch_name=f"tech-lead-investigation-6410-{TOKEN}",
        original_prompt="Investigate issue #6410",
        validation_error="boom",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
        validation_cmd="make test",
        authority_run=SessionRunIdentity(
            session_name="issue-6410", run_id="run-original", started_at="2026-09-18"
        ),
    )


class _Config:
    def __init__(
        self,
        repo_root: Path,
        worktree_base: Path,
        tech_lead_review_agent: str | None = "agent:tech-lead",
    ) -> None:
        self.repo_root = repo_root
        self.worktree_base = worktree_base
        self.tech_lead_review_agent = tech_lead_review_agent


def _audit(repo: Path, checkout: Path, state: OrchestratorState) -> tuple:
    return _reconciler(repo, checkout.parent, _Config).audit(state)


def test_a_checkout_with_a_queued_retry_is_not_a_cleanup_candidate(
    repo: Path, investigation: Path
) -> None:
    state = OrchestratorState()
    state.pending_validation_retries.append(_retry(investigation))

    entries = _audit(repo, investigation, state)

    assert [entry.disposition for entry in entries] == ["retained"]


def test_the_same_checkout_without_one_is(
    repo: Path, investigation: Path
) -> None:
    """The premise: this checkout IS otherwise disposable."""
    entries = _audit(repo, investigation, OrchestratorState())

    assert [entry.disposition for entry in entries] == ["cleanup_candidate"]


# ---------------------------------------------------------------------------
# The other half: a restart has to FIND the retry before it can protect it.
# ---------------------------------------------------------------------------

RUN_ID = "20260918T120000000000Z"
SESSION_NAME = "issue-6410"


def _leave_retry_artifacts(
    checkout: Path, *, agent_label: str = "agent:tech-lead"
) -> None:
    """Write what a run that ended in NEEDS_VALIDATION_RETRY leaves on disk."""
    run_dir = checkout / ".issue-orchestrator" / "sessions" / f"{RUN_ID}__{SESSION_NAME}"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": SESSION_NAME,
                "run_id": RUN_ID,
                "run_dir": str(run_dir),
                "issue_number": 6410,
                "agent_label": agent_label,
                "started_at": "2026-09-18T12:00:00+00:00",
            }
        )
    )
    (run_dir / "validation-state.json").write_text(
        json.dumps(
            {
                "retry_count": 1,
                "max_retries": 3,
                "validation_cmd": "make validate-quick",
                "last_error": "boom",
            }
        )
    )


def _recover(repo: Path, checkout: Path, state: OrchestratorState) -> int:
    """Run the startup recovery pass with no issue branches to lean on."""
    return ValidationRetryRecovery(
        _Config(repo, checkout.parent),
        _reconciler(repo, checkout.parent, _Config),
        lambda _name: False,
    ).recover(state, {})


def test_a_restart_re_queues_the_investigation_retry(
    repo: Path, investigation: Path
) -> None:
    """The checkout is on an unpushed branch under no issue number of its own."""
    _leave_retry_artifacts(investigation)
    state = OrchestratorState()

    assert _recover(repo, investigation, state) == 1

    [retry] = state.pending_validation_retries
    assert retry.issue_number == 6410
    assert retry.worktree_path == str(investigation)
    assert retry.branch_name == f"tech-lead-investigation-6410-{TOKEN}"
    assert retry.retry_count == 1


def test_the_re_queued_retry_still_names_its_launch_authority(
    repo: Path, investigation: Path
) -> None:
    """Without the original run, the resumed completion is `missing_authority`.

    The identity comes back off the run manifest, not out of memory: the process
    that queued the retry is gone.
    """
    _leave_retry_artifacts(investigation)
    state = OrchestratorState()

    _recover(repo, investigation, state)

    [retry] = state.pending_validation_retries
    assert retry.authority_run == SessionRunIdentity(
        session_name=SESSION_NAME,
        run_id=RUN_ID,
        started_at="2026-09-18T12:00:00+00:00",
    )


def test_recovery_then_reconciliation_keeps_the_branch(
    repo: Path, investigation: Path
) -> None:
    """End to end: the two halves, in the order a restart runs them."""
    _leave_retry_artifacts(investigation)
    state = OrchestratorState()

    _recover(repo, investigation, state)
    entries = _audit(repo, investigation, state)

    assert [entry.disposition for entry in entries] == ["retained"]


def test_an_ordinary_coder_retry_names_no_authority_run(
    repo: Path, investigation: Path
) -> None:
    """Recovery must reach the SAME answer as the live completion path.

    Deciding it locally -- "the manifest had an identity, so carry it" -- named
    a source run for every ordinary retry too. The launcher hard-refuses a
    retry whose named authority has no row, so every recovered coder retry sat
    in the queue forever, relaunched never (round 1 finding 2).
    """
    _leave_retry_artifacts(investigation, agent_label="agent:coder")
    state = OrchestratorState()

    assert _recover(repo, investigation, state) == 1

    [retry] = state.pending_validation_retries
    assert retry.authority_run is None, (
        "a coder retry named an authority row that was never recorded"
    )


def test_a_checkout_with_no_retry_artifacts_is_not_re_queued(
    repo: Path, investigation: Path
) -> None:
    """The premise: recovery is reading the artifacts, not the directory name."""
    state = OrchestratorState()

    assert _recover(repo, investigation, state) == 0
    assert state.pending_validation_retries == []
