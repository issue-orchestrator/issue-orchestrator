"""Real Git/SQLite/intake/escrow preservation across destructive boundaries."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import ConfiguredCompletionEvidenceValidator
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.control.issue_run_evidence import IssueRunEvidenceService
from issue_orchestrator.control.review_exchange_lifecycle import (
    CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners, OtherRuntimeActivity,
    IssueRuntimeOwnerKind, UnresolvedValidatedWork,
)
from issue_orchestrator.control.validated_work_capture import ParkedEvidenceCustody
from issue_orchestrator.control.validated_work_escrow import EscrowReconciliation
from issue_orchestrator.control.validated_work_preservation import ValidatedWorkPreservationService
from issue_orchestrator.domain.completion_intake import CompletionIntakeError, IntakeClosed
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidenceUnavailable
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.validated_work import ValidatedWorkFailure, ValidatedWorkState
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionGeneration
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import IsolatedCompletionValidationWorkspace
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
from issue_orchestrator.infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from tests.unit.test_completion_evidence_intake import completion, command


@pytest.fixture
def custody(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git = create_git(LocalCommandRunner())
    git.run(repo, ["init", "-b", "main"])
    git.run(repo, ["config", "user.name", "Preservation test"])
    git.run(repo, ["config", "user.email", "test@example.invalid"])
    (repo / "content").write_text("base")
    git.run(repo, ["add", "content"])
    git.run(repo, ["commit", "-m", "base"])
    worktree = tmp_path / "worktree"
    git.run(repo, ["worktree", "add", "-b", "feature", str(worktree)])
    state = tmp_path / "owner-state"
    ledger = SqliteIssueRunLedger(state / "runs.sqlite")
    wc = GitWorkingCopy(git=git)
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), ledger, wc)
    run = allocator.allocate(IssueRunAllocation(worktree, "issue-42", 42,
        SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.CODE), "agent:test", "test"))
    validator = ConfiguredCompletionEvidenceValidator(wc, LocalCommandRunner(),
        IsolatedCompletionValidationWorkspace(state, git), command="true", timeout_seconds=30)
    intake = CompletionEvidenceIntakeService(ledger, validator, Mock(spec=HistoricalIntakeHandler), Mock(spec=BackgroundJobRunner))
    escrow = FilesystemValidatedWorkEscrow(state / "validated-work", repository=repo, repo_slug="owner/repo", git=wc)
    store = SqliteValidatedWorkIntakeStore(state / "work.sqlite",
        GitValidatedWorkAncestry(repository=repo, repo_slug="owner/repo", git=wc), escrow)
    repair = EscrowReconciliation(escrow=escrow, store=store)
    preservation = ValidatedWorkPreservationService(intake=intake, store=store,
        custody=ParkedEvidenceCustody(escrow, store), repair=repair, working_copy=wc)
    source = IssueRunEvidenceService(ledger, live_runs=lambda issue: (), now=lambda: "2026-09-07T00:00:00Z")
    sessions = Mock()
    sessions.exists.return_value = False
    pair, jobs, retry = Mock(), Mock(), Mock()
    jobs.cancel_matching.return_value = ()
    core = CoreIssueRuntimeOwners(sessions, [], pair, jobs, retry)
    lifecycle = IssueRuntimeLifecycleOwners(core, preservation, source, Mock())
    return SimpleNamespace(repo=repo, git=git, worktree=worktree, ledger=ledger, run=run,
        capability=ledger.submission_capability(run), intake=intake, escrow=escrow,
        store=store, lifecycle=lifecycle, state=state, wc=wc, repair=repair,
        pair=pair, jobs=jobs, retry=retry, sessions=sessions)


def submit(rig, key):
    receipt = rig.intake.submit(rig.capability, command(completion(), key))
    rig.intake.drain()
    return receipt


def advance(rig):
    (rig.worktree / "content").write_text("advanced")
    rig.git.run(rig.worktree, ["add", "content"])
    rig.git.run(rig.worktree, ["commit", "-m", "advanced"])
    return rig.git.head_sha(rig.worktree)


def test_distinct_validated_heads_survive_and_newest_only_within_key(custody):
    first = submit(custody, "first")
    old_head = custody.ledger.validation_for_receipt(first.entry_id).head_sha
    advance(custody)
    second = submit(custody, "second")
    newest = submit(custody, "same-head-newer")
    result = custody.lifecycle.terminate(42, "stop")
    assert len(result.validated_work.dispositions) == 2
    rows = custody.store.retained_evidence(42)
    assert {r.admission.evidence.identity.key.validated_head_sha for r in rows} == {old_head, custody.git.head_sha(custody.worktree)}
    assert {r.admission.evidence.identity.completion_artifact.sha256 for r in rows} == {
        custody.ledger.entry_for_receipt(first.entry_id).normalized_sha256,
        custody.ledger.entry_for_receipt(newest.entry_id).normalized_sha256,
    }
    assert custody.ledger.entry_for_receipt(second.entry_id).normalized_sha256 not in {r.admission.evidence.identity.completion_artifact.sha256 for r in rows}
    assert all(row.admission.initial_state is ValidatedWorkState.PARKED for row in rows)
    assert all(custody.escrow.verifies(row) for row in rows)


def test_advance_pins_validated_and_observed_separately_and_retry_preserves_observations(custody):
    receipt = submit(custody, "first")
    validated = custody.ledger.validation_for_receipt(receipt.entry_id).head_sha
    observed = advance(custody)
    batch = custody.lifecycle.terminate(42, "stop").validated_work
    row = custody.store.retained_evidence(42)[0]
    assert row.admission.evidence.identity.key.validated_head_sha == validated
    assert row.admission.evidence.observations.worktree_head_sha == observed
    assert row.admission.initial_failure is ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION
    custody.git.run(custody.repo, ["worktree", "remove", "--force", str(custody.worktree)])
    custody.git.run(custody.repo, ["gc", "--prune=now"])
    assert custody.escrow.verifies(row)
    assert custody.lifecycle.terminate(42, "repeat").validated_work == batch
    assert custody.store.retained_evidence(42)[0].admission == row.admission


def test_detached_keeps_exact_head_and_requires_approval(custody):
    submit(custody, "first")
    custody.git.run(custody.worktree, ["checkout", "--detach"])
    custody.lifecycle.terminate(42, "stop")
    row = custody.store.retained_evidence(42)[0]
    assert not row.admission.evidence.identity.branch_binding_verified
    assert row.admission.evidence.identity.key.branch_name == "feature"
    assert row.admission.initial_failure is ValidatedWorkFailure.WORKSPACE_INTEGRITY


@pytest.mark.parametrize("damage", ["raw", "normalized", "validation", "ledger", "escrow", "pin"])
def test_custody_failure_prevents_every_destructive_owner(custody, damage):
    receipt = submit(custody, "first")
    if damage in {"escrow", "pin"}:
        custody.lifecycle.preserve(42, "capture")
        row = custody.store.retained_evidence(42)[0]
        if damage == "escrow":
            (custody.state / "validated-work" / row.admission.escrow_dir / "completion.json").unlink()
        else:
            custody.git.run(custody.repo, ["update-ref", "-d", row.admission.pinned_ref])
    elif damage == "ledger":
        (custody.state / "runs.sqlite").unlink()
    else:
        entry = custody.ledger.entry_for_receipt(receipt.entry_id)
        path = {"raw": entry.raw_path, "normalized": entry.normalized_path,
            "validation": custody.ledger.validation_for_receipt(receipt.entry_id).result_path}[damage]
        path.write_bytes(b"corrupt")
    with pytest.raises((CompletionIntakeError, IssueRunEvidenceUnavailable, ValueError)):
        custody.lifecycle.terminate(42, "unsafe")
    custody.pair.release.assert_not_called()
    custody.jobs.cancel_matching.assert_not_called()
    custody.retry.abandon_issue.assert_not_called()
    custody.sessions.stop.assert_not_called()


def test_missing_object_fails_before_teardown_and_retains_receipts(custody):
    receipt = submit(custody, "first")
    head = custody.ledger.validation_for_receipt(receipt.entry_id).head_sha
    obj = custody.repo / ".git" / "objects" / head[:2] / head[2:]
    obj.unlink()
    with pytest.raises(Exception):
        custody.lifecycle.terminate(42, "stop")
    assert custody.ledger.entry_for_receipt(receipt.entry_id).raw_path.exists()
    custody.pair.release.assert_not_called()


def test_partial_batch_admission_retry_converges(custody):
    submit(custody, "first")
    advance(custody)
    submit(custody, "second")
    with sqlite3.connect(custody.state / "work.sqlite") as conn:
        conn.execute("CREATE TRIGGER interrupt_second BEFORE INSERT ON validated_work_records WHEN (SELECT COUNT(*) FROM validated_work_records)=1 BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
    with pytest.raises(CompletionIntakeError):
        custody.lifecycle.terminate(42, "stop")
    assert len(custody.store.for_issue(42).dispositions) == 1
    custody.pair.release.assert_not_called()
    with sqlite3.connect(custody.state / "work.sqlite") as conn:
        conn.execute("DROP TRIGGER interrupt_second")
    batch = custody.lifecycle.terminate(42, "retry").validated_work
    assert len(batch.dispositions) == 2
    assert len(custody.store.retained_evidence(42)) == 2


def test_scratch_reset_refuses_parked_but_normal_stop_can_release(custody):
    submit(custody, "first")
    with pytest.raises(UnresolvedValidatedWork) as failure:
        custody.lifecycle.require_reset(42, "reset")
    assert failure.value.batch.unresolved
    assert custody.lifecycle.has_active_issue_runtime(42)
    custody.pair.release.assert_not_called()
    assert custody.lifecycle.terminate(42, "ordinary-stop").validated_work.unresolved
    custody.pair.release.assert_called_once()


def test_manual_exact_receipt_preparation_does_not_close_run(custody):
    receipt = submit(custody, "first")
    candidate = custody.intake.prepare_receipt(receipt, custody.run)
    assert candidate.validation.head_sha == custody.git.head_sha(custody.worktree)
    with pytest.raises(CompletionIntakeError, match="allocated run"):
        custody.intake.prepare_receipt(replace(receipt, content_sha256="0" * 64), custody.run)
    from tests.unit.test_issue_run_evidence import run_record
    other = run_record(custody.worktree.parent / "other").run
    with pytest.raises(CompletionIntakeError, match="allocated run"):
        custody.intake.prepare_receipt(receipt, other)
    submit(custody, "still-open")
    custody.lifecycle.preserve(42, "stop")
    with pytest.raises(IntakeClosed):
        submit(custody, "now-closed")


def test_canonical_ingestion_damage_does_not_hide_retained_candidate(custody):
    receipt = submit(custody, "first")
    certified = custody.run.run_dir / "completion-intake" / receipt.entry_id / "validation.json"
    certified.write_bytes(b"invalid canonical copy")
    with pytest.raises(CompletionIntakeError):
        custody.intake.require_publication_ready(receipt, custody.run)
    assert custody.lifecycle.terminate(42, "stop").validated_work.unresolved


def test_probe_evaluates_every_owner_and_names_unverifiable(custody):
    custody.sessions.exists.return_value = True
    custody.pair.has_active_pair.side_effect = RuntimeError("unavailable")
    custody.jobs.has_matching.return_value = False
    custody.retry.has_active_retry.return_value = False
    probe = custody.lifecycle.probe(42)
    assert probe.active == frozenset({IssueRuntimeOwnerKind.SESSIONS})
    assert probe.unverifiable == frozenset({IssueRuntimeOwnerKind.EXCHANGE_PAIR})
    custody.retry.has_active_retry.assert_called_once_with(42)
    custody.jobs.has_matching.assert_called_once()
    assert OtherRuntimeActivity(custody.lifecycle.core).core is custody.lifecycle.core


def test_stale_generation_does_not_close_replacement_intake(custody):
    active = SimpleNamespace(issue=SimpleNamespace(number=42), key=SimpleNamespace(task=TaskKind.CODE),
        terminal_id="issue-42", run_assets=custody.run)
    custody.lifecycle.core.active_sessions.append(active)
    target = TechLeadSessionGeneration(42, TaskKind.CODE, "issue-42", "old-generation")
    stop = Mock()
    result = custody.lifecycle.terminate_generation(target, "kill", session_exists=Mock(return_value=True), kill_session=stop)
    assert result.stale_reason
    stop.assert_not_called()
    submit(custody, "replacement-open")


def test_reset_snapshot_and_downgrade_keep_each_retained_member(custody):
    from issue_orchestrator.control.tech_lead_reset_retry import TechLeadResetRetryExecutor
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.infra.config import Config
    from tests.unit.control.test_tech_lead_reset_retry import make_action, make_issue
    submit(custody, "first")
    advance(custody)
    submit(custody, "second")
    batch = custody.lifecycle.preserve(42, "stop")
    run_reset = Mock()
    events = Mock()
    executor = TechLeadResetRetryExecutor(events, LabelManager(Config()),
        lambda issue: make_issue(number=issue), custody.lifecycle.reset_snapshot, run_reset)
    result = executor.apply(make_action(issue_number=42))
    run_reset.assert_not_called()
    observed = result.details["boundary"]["validated_work"]["dispositions"]
    assert {row["evidence_id"] for row in observed} == {row.evidence_id for row in batch.dispositions}
    assert len(observed) == 2
    assert events.publish.call_args.args[0].data["boundary"] == result.details["boundary"]


def test_worktree_cleanup_requires_exact_owner_and_complete_custody(custody):
    submit(custody, "retained")
    with pytest.raises(RuntimeError, match="No trusted run ownership"):
        custody.lifecycle.preserve_worktree(custody.worktree.parent / "lookalike", "cleanup")
    batches = custody.lifecycle.preserve_worktree(custody.worktree, "cleanup")
    assert len(batches) == 1
    assert batches[0].unresolved
    custody.git.run(custody.repo, ["worktree", "remove", "--force", str(custody.worktree)])
    assert custody.lifecycle.preserve_worktree(custody.worktree, "retry")[0].dispositions == batches[0].dispositions
