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
from unittest.mock import MagicMock

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.issue_run_evidence import (
    IssueRunRecord,
    RunTerminalBinding,
)
from issue_orchestrator.domain.models import OrchestratorState, PendingValidationRetry
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets, SessionRunIdentity
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)
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


def _run_assets(checkout: Path) -> SessionRunAssets:
    """The run assets the ALLOCATOR durably recorded for this retry."""
    run_dir = checkout / ".issue-orchestrator" / "sessions" / f"{RUN_ID}__{SESSION_NAME}"
    run_dir.mkdir(parents=True, exist_ok=True)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("")
    return SessionRunAssets.from_paths(
        session_name=SESSION_NAME,
        run_id=RUN_ID,
        worktree_path=checkout,
        run_dir=run_dir,
        terminal_recording_path=recording,
        manifest_path=run_dir / "manifest.json",
        started_at="2026-09-18T12:00:00+00:00",
    )


def _ledger(checkout: Path, *, agent_label: str, completion_task: TaskKind):
    """A durable issue-run ledger holding this retry's exact allocation."""
    ledger = MagicMock()
    ledger.recorded_runs.return_value = (
        IssueRunRecord(
            session_key=SessionKey(FakeIssueKey("6410"), TaskKind.CODE),
            run=_run_assets(checkout),
            recorded_at="2026-09-18T12:00:00+00:00",
            branch_name=f"tech-lead-investigation-6410-{TOKEN}",
            terminal_binding=RunTerminalBinding("issue-6410"),
            agent_label=agent_label,
            completion_task=completion_task,
        ),
    )
    return ledger


def _authority_store(*, grant: bool = True) -> InMemoryTechLeadAuthorityStore:
    store = InMemoryTechLeadAuthorityStore()
    if grant:
        store.record(
            run_id=RUN_ID,
            session_name=SESSION_NAME,
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=6410,
                focus_issue_number=6410,
            ),
        )
    return store


def _recover(
    repo: Path,
    checkout: Path,
    state: OrchestratorState,
    *,
    agent_label: str = "agent:tech-lead",
    completion_task: TaskKind = TaskKind.TECH_LEAD,
    authority: "InMemoryTechLeadAuthorityStore | None" = None,
) -> int:
    """Run the startup recovery pass with no issue branches to lean on."""
    return ValidationRetryRecovery(
        _Config(repo, checkout.parent),
        _reconciler(repo, checkout.parent, _Config),
        lambda _name: False,
        _ledger(checkout, agent_label=agent_label, completion_task=completion_task),
        authority if authority is not None else _authority_store(),
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

    assert (
        _recover(
            repo,
            investigation,
            state,
            agent_label="agent:coder",
            completion_task=TaskKind.CODE,
            authority=_authority_store(grant=False),
        )
        == 1
    )

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


# ---------------------------------------------------------------------------
# The manifest inside the run directory is AGENT-WRITABLE. Authority-bearing
# facts come from the orchestrator's own ledger, joined on the canonical
# directory name (round 3 finding 1).
# ---------------------------------------------------------------------------


def test_an_edited_manifest_cannot_select_another_runs_grant(
    repo: Path, investigation: Path
) -> None:
    """It names another retained run and lies about the role. Both are ignored."""
    _leave_retry_artifacts(investigation)
    authority = _authority_store()
    authority.record(
        run_id="other-retained-run",
        session_name=SESSION_NAME,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=9999,
            focus_issue_number=9999,
        ),
    )
    run_dir = (
        investigation / ".issue-orchestrator" / "sessions" / f"{RUN_ID}__{SESSION_NAME}"
    )
    payload = json.loads((run_dir / "manifest.json").read_text())
    payload["run_id"] = "other-retained-run"
    payload["agent_label"] = "agent:coder"
    (run_dir / "manifest.json").write_text(json.dumps(payload))
    state = OrchestratorState()

    assert _recover(repo, investigation, state, authority=authority) == 1

    [retry] = state.pending_validation_retries
    assert retry.authority_run is not None
    assert retry.authority_run.run_id == RUN_ID, (
        "the retry inherited the grant the edited manifest pointed at"
    )
    assert retry.agent_label == "agent:tech-lead", (
        "the retry took its role from the manifest instead of the ledger"
    )
    assert retry.recovery_error is None


def test_a_missing_launch_authority_is_named_not_silently_dropped(
    repo: Path, investigation: Path
) -> None:
    """"Required but damaged" must not read as "ordinary retry that needs none".

    The retry stays QUEUED either way -- its checkout and artifact holds are the
    only remaining protection for the work -- but the launcher has to be able to
    tell the two apart, and refuse before it spends a session.
    """
    _leave_retry_artifacts(investigation)
    state = OrchestratorState()

    assert (
        _recover(repo, investigation, state, authority=_authority_store(grant=False))
        == 1
    )

    [retry] = state.pending_validation_retries
    assert "original launch authority is missing" in (retry.recovery_error or "")
    assert _audit(repo, investigation, state)[0].disposition == "retained", (
        "the checkout stopped being protected the moment recovery found damage"
    )


def test_a_run_directory_with_no_durable_allocation_is_refused(
    repo: Path, investigation: Path
) -> None:
    """The premise: the ledger, not the directory, is what makes a run real."""
    _leave_retry_artifacts(investigation)
    empty = MagicMock()
    empty.recorded_runs.return_value = ()
    state = OrchestratorState()

    recovered = ValidationRetryRecovery(
        _Config(repo, investigation.parent),
        _reconciler(repo, investigation.parent, _Config),
        lambda _name: False,
        empty,
        _authority_store(),
    ).recover(state, {})

    assert recovered == 1
    [retry] = state.pending_validation_retries
    assert "0 exact durable allocation records" in (retry.recovery_error or "")


def test_the_join_is_exact_not_merely_the_first_run_of_the_issue(
    repo: Path, investigation: Path
) -> None:
    """The ledger holds EVERY run of an issue, not just this retry's.

    Matching loosely -- "a run of issue 6410" -- would attach whichever record
    came first. The join is on the canonical run key AND the run directory AND
    the checkout, so a second run of the same issue is simply not this one.
    """
    _leave_retry_artifacts(investigation)
    other_dir = (
        investigation
        / ".issue-orchestrator"
        / "sessions"
        / "20260101T000000000000Z__issue-6410"
    )
    other_dir.mkdir(parents=True)
    other_recording = other_dir / "terminal-recording.jsonl"
    other_recording.write_text("")
    other = IssueRunRecord(
        session_key=SessionKey(FakeIssueKey("6410"), TaskKind.CODE),
        run=SessionRunAssets.from_paths(
            session_name=SESSION_NAME,
            run_id="20260101T000000000000Z",
            worktree_path=investigation,
            run_dir=other_dir,
            terminal_recording_path=other_recording,
            manifest_path=other_dir / "manifest.json",
            started_at="2026-01-01T00:00:00+00:00",
        ),
        recorded_at="2026-01-01T00:00:00+00:00",
        branch_name=f"tech-lead-investigation-6410-{TOKEN}",
        terminal_binding=RunTerminalBinding("issue-6410"),
        agent_label="agent:coder",
        completion_task=TaskKind.CODE,
    )
    ledger = _ledger(
        investigation, agent_label="agent:tech-lead", completion_task=TaskKind.TECH_LEAD
    )
    # The OTHER run first, so a loose match would take it.
    ledger.recorded_runs.return_value = (other,) + ledger.recorded_runs.return_value
    state = OrchestratorState()

    recovered = ValidationRetryRecovery(
        _Config(repo, investigation.parent),
        _reconciler(repo, investigation.parent, _Config),
        lambda _name: False,
        ledger,
        _authority_store(),
    ).recover(state, {})

    assert recovered == 1
    [retry] = state.pending_validation_retries
    assert retry.recovery_error is None, retry.recovery_error
    assert retry.agent_label == "agent:tech-lead", (
        "an unrelated run of the same issue supplied the role"
    )
    assert retry.authority_run is not None
    assert retry.authority_run.run_id == RUN_ID
