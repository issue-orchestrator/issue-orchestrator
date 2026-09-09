"""Real Git/SQLite/intake/escrow preservation across destructive boundaries."""

from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission

from issue_orchestrator.infra.config import Config

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
from issue_orchestrator.control.validated_work_capture import ValidatedWorkCustody
from issue_orchestrator.control.validated_work_escrow import EscrowReconciliation
from issue_orchestrator.control.validated_work_preservation import ValidatedWorkPreservationService
from issue_orchestrator.domain.completion_intake import CompletionIntakeError, IntakeClosed
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidenceUnavailable, RunTerminalBinding
from issue_orchestrator.domain.publication_remote import (
    PublicationPullRequest, PublicationPrState, PublicationRemoteError,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus, ValidatedWorkFailure, ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_capture import ValidatedWorkRemoteFacts
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionGeneration
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import IsolatedCompletionValidationWorkspace
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from issue_orchestrator.ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from tests.unit.test_completion_evidence_intake import completion, command
from tests.unit.validated_work_support import Liveness


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
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), ledger, wc, configuration=Config(repo="owner/repo"))
    run = allocator.allocate(IssueRunAllocation(worktree, "coding-1", 42,
        SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.CODE), "agent:test", "test", terminal_id="issue-42"))
    validator = ConfiguredCompletionEvidenceValidator(wc, LocalCommandRunner(),
        IsolatedCompletionValidationWorkspace(state, git), command="true", timeout_seconds=30)
    intake = CompletionEvidenceIntakeService(ledger, validator, Mock(spec=HistoricalIntakeHandler), Mock(spec=BackgroundJobRunner))
    escrow = FilesystemValidatedWorkEscrow(state / "validated-work", repository=repo, repo_slug="owner/repo", git=wc)
    store = SqliteValidatedWorkStore(
        state / "work.sqlite",
        ancestry=GitValidatedWorkAncestry(repository=repo, repo_slug="owner/repo", git=wc),
        artifacts=escrow, liveness=Liveness(), retention=escrow,
    )
    store = RankedEvidenceAdmission(store, ledger)
    repair = EscrowReconciliation(escrow=escrow, store=store)
    observer = Mock(spec=ValidatedWorkCaptureObserver)
    observer.observe.return_value = ValidatedWorkRemoteFacts(None, ())
    preservation = ValidatedWorkPreservationService(intake=intake, store=store,
        custody=ValidatedWorkCustody(escrow, store), repair=repair, working_copy=wc,
        observer=observer)
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
        pair=pair, jobs=jobs, retry=retry, sessions=sessions, observer=observer)


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
    assert {
        row.admission.evidence.identity.key.validated_head_sha: row.admission.initial_state
        for row in rows
    } == {
        old_head: ValidatedWorkState.PARKED,
        custody.git.head_sha(custody.worktree): ValidatedWorkState.QUEUED,
    }
    assert all(custody.escrow.verifies(row) for row in rows)
    custody.observer.observe.assert_called_once()


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


def test_unreadable_remote_parks_without_inventing_branch_absence(custody):
    submit(custody, "first")
    custody.observer.observe.side_effect = PublicationRemoteError("quota or auth unavailable")
    custody.lifecycle.terminate(42, "stop")
    row = custody.store.retained_evidence(42)[0]
    observations = row.admission.evidence.observations
    assert row.admission.initial_state is ValidatedWorkState.PARKED
    assert row.admission.initial_failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert observations.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED
    assert observations.expected_remote_head_sha is None
    assert observations.pr_number is None


def test_observed_branch_and_single_matching_pr_become_automatic_authority(custody):
    receipt = submit(custody, "first")
    head = custody.ledger.validation_for_receipt(receipt.entry_id).head_sha
    pr = PublicationPullRequest(
        91, "https://github.com/owner/repo/pull/91", "owner/repo", "owner/repo",
        "feature", "main", head, PublicationPrState.OPEN, "existing PR",
    )
    custody.observer.observe.return_value = ValidatedWorkRemoteFacts(head, (pr,))
    custody.lifecycle.terminate(42, "stop")
    row = custody.store.retained_evidence(42)[0]
    observations = row.admission.evidence.observations
    assert row.admission.initial_state is ValidatedWorkState.QUEUED
    assert observations.remote_baseline_status is RemoteBaselineStatus.OBSERVED
    assert observations.expected_remote_head_sha == head
    assert observations.pr_number == 91


def test_multiple_open_prs_park_instead_of_guessing(custody):
    receipt = submit(custody, "first")
    head = custody.ledger.validation_for_receipt(receipt.entry_id).head_sha
    def pr(number):
        return PublicationPullRequest(
            number, f"https://github.com/owner/repo/pull/{number}",
            "owner/repo", "owner/repo", "feature", "main", head,
            PublicationPrState.OPEN, "candidate",
        )
    custody.observer.observe.return_value = ValidatedWorkRemoteFacts(head, (pr(91), pr(92)))
    custody.lifecycle.terminate(42, "stop")
    row = custody.store.retained_evidence(42)[0]
    assert row.admission.initial_state is ValidatedWorkState.PARKED
    assert row.admission.initial_failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert row.admission.evidence.observations.pr_number is None


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
    with pytest.raises((CompletionIntakeError, sqlite3.IntegrityError)):
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


def test_startup_publishing_worktree_captures_before_removal(custody):
    from issue_orchestrator.control.worktree_reconciliation import StartupWorktreeReconciler, WorktreeAuditEntry
    from issue_orchestrator.domain.models import OrchestratorState
    submit(custody, "retained")
    manager, audit, cleanup = Mock(), Mock(), Mock()
    cleanup.recover_orphaned_cleanups.return_value = 0
    audit.audit.return_value = (WorktreeAuditEntry(custody.worktree, "tech_lead_scratch", "cleanup_candidate", "orphan"),)
    def remove(path, *, force):
        assert custody.store.for_issue(42).unresolved
        custody.git.run(custody.repo, ["worktree", "remove", "--force", str(path)])
    manager.remove_checkout_and_branch.side_effect = remove
    reconciler = StartupWorktreeReconciler(SimpleNamespace(repo_root=custody.repo, worktree_base=custody.worktree.parent),
        cleanup, manager, audit, custody.lifecycle)
    assert reconciler.recover(OrchestratorState()).disposable_removed == 1
    assert not custody.worktree.exists()


def test_startup_publishing_worktree_retained_when_custody_unavailable(custody):
    from issue_orchestrator.control.worktree_reconciliation import StartupWorktreeReconciler, WorktreeAuditEntry
    from issue_orchestrator.domain.models import OrchestratorState
    submit(custody, "retained")
    custody.lifecycle.preserve(42, "first")
    manager, audit, cleanup = Mock(), Mock(), Mock()
    cleanup.recover_orphaned_cleanups.return_value = 0
    audit.audit.return_value = (WorktreeAuditEntry(custody.worktree.parent / "unknown", "tech_lead_scratch", "cleanup_candidate", "orphan"),)
    reconciler = StartupWorktreeReconciler(SimpleNamespace(repo_root=custody.repo, worktree_base=custody.worktree.parent),
        cleanup, manager, audit, custody.lifecycle)
    assert reconciler.recover(OrchestratorState()).retained == 1
    manager.remove_checkout_and_branch.assert_not_called()


@pytest.mark.parametrize("scope", ["exact_run", "named_terminal"])
def test_terminal_preservation_keeps_unrelated_allocated_run_open(custody, scope):
    review = IssueRunAllocationService(FileSystemSessionOutput(), custody.ledger, custody.wc, configuration=Config(repo="owner/repo")).allocate(
        IssueRunAllocation(custody.worktree, "review-phase-1", 42,
            SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.REVIEW), "agent:test", "test", terminal_id="review-42"))
    review_capability = custody.ledger.submission_capability(review)
    receipt = custody.intake.submit(review_capability, command(completion(), "review-receipt"))
    custody.intake.prepare_receipt(receipt, review)
    if scope == "exact_run":
        assert custody.lifecycle.preserve_terminal(42, "review-42", "completed", run=review).unresolved
    else:
        assert custody.lifecycle.preserve_named_terminal("review-42", "completed")[0].unresolved
    with pytest.raises(IntakeClosed):
        custody.intake.submit(review_capability, command(completion(), "review-after-close"))
    submit(custody, "coder-still-open")


def test_stop_committed_then_raised_preserves_batch(custody):
    from issue_orchestrator.control.review_exchange_lifecycle import GenerationTerminationPartialFailure
    submit(custody, "retained")
    active = SimpleNamespace(issue=SimpleNamespace(number=42), key=SimpleNamespace(task=TaskKind.CODE),
        terminal_id="issue-42", run_assets=custody.run)
    custody.lifecycle.core.active_sessions.append(active)
    running = True
    def stop(terminal):
        nonlocal running
        running = False
        raise RuntimeError("post-stop observer failed")
    with pytest.raises(GenerationTerminationPartialFailure) as raised:
        custody.lifecycle.terminate_generation(TechLeadSessionGeneration(42, TaskKind.CODE, "issue-42", custody.run.run_id),
            "kill", session_exists=lambda terminal: running, kill_session=stop)
    assert raised.value.validated_work == custody.store.for_issue(42)
    assert custody.lifecycle.core.active_sessions == []


def test_shutdown_unknown_live_run_refuses_before_any_global_stop(custody):
    from issue_orchestrator.domain.issue_run_evidence import IssueRunRecord
    unrecorded = FileSystemSessionOutput().start_run(custody.worktree, "issue-99")
    live = IssueRunRecord(SessionKey(GitHubIssueKey("owner/repo", "99"), TaskKind.CODE), unrecorded, "2026-09-07", "feature", RunTerminalBinding("issue-99"))
    source = IssueRunEvidenceService(custody.ledger, live_runs=lambda issue: (live,) if issue == 99 else (), now=lambda: "2026-09-07")
    custody.lifecycle.core.active_sessions.append(SimpleNamespace(issue=SimpleNamespace(number=99)))
    lifecycle = replace(custody.lifecycle, run_evidence=source)
    runner = Mock()
    with pytest.raises(IssueRunEvidenceUnavailable):
        lifecycle.shutdown(runner)
    runner.on_orchestrator_shutdown.assert_not_called()
    custody.pair.shutdown_all.assert_not_called()


def test_review_worktree_cleanup_preserves_other_terminal_evidence(custody):
    submit(custody, "coder-retained")
    review = IssueRunAllocationService(FileSystemSessionOutput(), custody.ledger, custody.wc, configuration=Config(repo="owner/repo")).allocate(
        IssueRunAllocation(custody.worktree, "review-phase-1", 42,
            SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.REVIEW), "agent:test", "test", terminal_id="review-42"))
    batch = custody.lifecycle.preserve_cleanup(42, review.session_name, custody.worktree, "cleanup")
    assert batch.unresolved
    custody.git.run(custody.repo, ["worktree", "remove", "--force", str(custody.worktree)])
    assert custody.lifecycle.preserve_worktree(custody.worktree, "retry")[0].dispositions == batch.dispositions


def preserve_receipts_in_reverse_order(custody):
    submit(custody, "older")
    exchange = IssueRunAllocationService(FileSystemSessionOutput(), custody.ledger, custody.wc, configuration=Config(repo="owner/repo")).allocate(
        IssueRunAllocation(custody.worktree, "exchange-42", 42,
            SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.CODE), "agent:test", "test", terminal_id="issue-42"))
    import json
    new_completion = json.loads(completion())
    new_completion["implementation"] = "newer exact receipt"
    receipt = custody.intake.submit(custody.ledger.submission_capability(exchange), command(json.dumps(new_completion).encode(), "newer"))
    custody.intake.prepare_receipt(receipt, exchange)
    newer = custody.lifecycle.preserve_terminal(42, exchange.session_name, "newer", run=exchange)
    older = custody.lifecycle.preserve_terminal(42, custody.run.session_name, "older", run=custody.run)
    return newer, older


def test_later_capture_cannot_replace_newer_receipt_for_same_key(custody):
    newer, older = preserve_receipts_in_reverse_order(custody)
    assert older.dispositions[0].evidence_id == newer.dispositions[0].evidence_id


def retained_receipt_pair(custody):
    preserve_receipts_in_reverse_order(custody)
    return tuple(sorted((row.admission for row in custody.store.retained_evidence(42)),
        key=lambda admission: custody.ledger.evidence_receive_sequence(admission.evidence)))


def independent_ranked_store(custody, database_name, backend_type=SqliteValidatedWorkStore):
    ledger = SqliteIssueRunLedger(custody.state / "runs.sqlite")
    ancestry = GitValidatedWorkAncestry(
        repository=custody.repo, repo_slug="owner/repo", git=custody.wc,
    )
    backend = backend_type(
        custody.state / database_name, ancestry=ancestry, artifacts=custody.escrow,
        liveness=Liveness(), retention=custody.escrow,
    )
    return RankedEvidenceAdmission(backend, ledger)


def test_independent_instances_reselect_after_atomic_admission_conflict(custody):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    admissions = retained_receipt_pair(custody)
    barrier = Barrier(2)
    class ConcurrentBackend(SqliteValidatedWorkStore):
        def admit_selected(self, admission, expected_current, selection):
            if expected_current is None:
                barrier.wait(timeout=10)
            return super().admit_selected(admission, expected_current, selection)
    stores = [independent_ranked_store(custody, "concurrent.sqlite", ConcurrentBackend) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].admit(pair[1]), zip(stores, admissions)))
    assert len(results) == 2
    reopened = independent_ranked_store(custody, "concurrent.sqlite")
    assert reopened.for_issue(42).dispositions[0].evidence_id == admissions[-1].evidence.evidence_id
    assert len(reopened.retained_evidence(42)) == 2


def test_orphan_repair_and_restart_keep_trusted_receipt_order(custody):
    admissions = retained_receipt_pair(custody)
    recovered = independent_ranked_store(custody, "recovered.sqlite")
    report = EscrowReconciliation(escrow=custody.escrow, store=recovered).reconcile_escrow_orphans()
    assert not report.problems
    reopened = independent_ranked_store(custody, "recovered.sqlite")
    assert reopened.for_issue(42).dispositions[0].evidence_id == admissions[-1].evidence.evidence_id
    reopened.admit(admissions[0])
    assert reopened.for_issue(42).dispositions[0].evidence_id == admissions[-1].evidence.evidence_id


def test_unmapped_evidence_cannot_invent_receive_precedence(custody):
    admissions = retained_receipt_pair(custody)
    invented = replace(admissions[0], evidence=replace(admissions[0].evidence,
        identity=replace(admissions[0].evidence.identity, completion_artifact=replace(
            admissions[0].evidence.identity.completion_artifact, sha256="0" * 64))))
    with pytest.raises(CompletionIntakeError, match="receive-order proof"):
        custody.store.admit(invented)
    assert custody.store.for_issue(42).dispositions[0].evidence_id == admissions[-1].evidence.evidence_id


def test_receive_order_uses_exact_attestation_and_idempotent_receipt(custody):
    first = submit(custody, "first")
    replay = custody.intake.submit(custody.capability, command(completion(), "first"))
    assert replay == first
    second = submit(custody, "identical-completion-new-receipt")
    assert custody.ledger.entry_for_receipt(second.entry_id).receive_sequence > custody.ledger.entry_for_receipt(first.entry_id).receive_sequence
    custody.lifecycle.preserve(42, "stop")
    evidence = custody.store.retained_evidence(42)[0].admission.evidence
    assert custody.ledger.evidence_receive_sequence(evidence) == custody.ledger.entry_for_receipt(second.entry_id).receive_sequence
