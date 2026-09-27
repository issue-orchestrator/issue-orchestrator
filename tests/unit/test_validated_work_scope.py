"""Validated-work recovery owns coding/rework completions only (#7323).

Real Git, SQLite, intake, escrow and aggregate recovery-block composition; only
GitHub labels are a fake. The tech-lead run is recorded exactly as porchpin#410's
was: session key task ``code`` on the anchor issue, allocated to the configured
tech-lead agent, so its ``completion_task`` is ``tech-lead``.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from tests.unit.control.liveness_doubles import drain_liveness
from issue_orchestrator.adapters.issue_disposition_gate import FileIssueDispositionMutationGate
from issue_orchestrator.control import validated_work_preservation
from issue_orchestrator.control.actions import ActionResult, AddLabelAction
from issue_orchestrator.control.aggregate_recovery_block import AggregateRecoveryBlocks
from issue_orchestrator.control.claimed_recovery_preparation import ClaimedRecoveryPreparation
from issue_orchestrator.control.completion_handler import CleanupDecision, SessionStatus
from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import ConfiguredCompletionEvidenceValidator
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.control.issue_run_evidence import IssueRunEvidenceService
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NO_OTHER_NEEDS_HUMAN_CAUSES, NeedsHumanBlock
from issue_orchestrator.control.published_review_custody import PublishedReviewCustody
from issue_orchestrator.control.recovery_publication_attempt import RecoveryPublicationAttempt
from issue_orchestrator.control.recovery_publication_completion import RecoveryPublicationCompletion
from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.control.recovery_record_operation import RecoveryRecordOperation
from issue_orchestrator.control.remote_authority_refresh import RemoteAuthorityRefreshOperation
from issue_orchestrator.control.review_exchange_lifecycle import (
    CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners,
)
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission
from issue_orchestrator.control.validated_work_capture import ValidatedWorkCustody
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.control.validated_work_escrow import EscrowReconciliation
from issue_orchestrator.control.validated_work_preservation import ValidatedWorkPreservationService
from issue_orchestrator.control.validated_work_scope_retirement import (
    RETIREMENT_ACTOR, OutOfScopeRecordRetirement, OutOfScopeRetirementSweep,
)
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.models import OrchestratorState, SessionHistoryEntry
from issue_orchestrator.domain.publication_remote import PublicationRemoteError
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest
from issue_orchestrator.domain.registered_completion import (
    CompletionProcessingPolicy, CompletionRunRole,
)
from issue_orchestrator.domain.session_key import SessionKey
from issue_orchestrator.domain.session_kind import HISTORICAL_AGENT_LABEL, SessionKind
from issue_orchestrator.domain.validated_work import (
    DispositionPhase, PublishValidatedHeadStatus, RemoteBaselineStatus, ResolutionKind,
    ValidatedWorkFailure, ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_capture import ValidatedWorkRemoteFacts
from issue_orchestrator.domain.validated_work_scope import (
    outside_scope_reason, recovery_owns,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import IsolatedCompletionValidationWorkspace
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
from issue_orchestrator.execution.validated_work_execution import LocalValidatedWorkExecutionOwner
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from issue_orchestrator.ports.recovery_block import NullRecoveryBlockSweep
from issue_orchestrator.ports.retained_claim_maintenance import NullRetainedClaimMaintenance
from issue_orchestrator.ports.validated_work_drain import NullValidatedWorkScopeSweep
from issue_orchestrator.ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from tests.runtime_lifecycle_helpers import no_open_pull_requests
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.validated_work_support import Liveness, Rig, capture, owned_intake

ISSUE = 410
TECH_LEAD = "agent:tech-lead"
CODER = "agent:coder"
RECOVERY_PENDING = "recovery-pending"


class Labels:
    """GitHub's view of the issue's labels: the one fake in this composition."""

    def __init__(self, issue: int = ISSUE) -> None:
        self.issue = issue
        self.labels: set[str] = set()
        self.operations: list[tuple[str, str]] = []

    def read_issue_labels(self, issue_number):
        assert issue_number == self.issue
        return sorted(self.labels)

    def add_label(self, issue_number, label):
        assert issue_number == self.issue
        self.labels.add(label)
        self.operations.append(("add", label))

    def remove_label(self, issue_number, label):
        assert issue_number == self.issue
        self.labels.discard(label)
        self.operations.append(("remove", label))

    def apply(self, action):
        assert action.issue_number == self.issue
        add = isinstance(action, AddLabelAction)
        (self.labels.add if add else self.labels.discard)(action.label)
        self.operations.append(("add" if add else "remove", action.label))
        return ActionResult.ok(action)


def _rig(tmp_path, agent_label):
    repo, worktree, state = tmp_path / "repository", tmp_path / "worktree", tmp_path / "owner-state"
    repo.mkdir()
    git = create_git(LocalCommandRunner())
    git.run(repo, ["init", "-b", "main"])
    git.run(repo, ["config", "user.name", "Scope test"])
    git.run(repo, ["config", "user.email", "test@example.invalid"])
    (repo / "content").write_text("base")
    git.run(repo, ["add", "content"])
    git.run(repo, ["commit", "-m", "base"])
    git.run(repo, ["worktree", "add", "-b", f"{ISSUE}-health-review-walk-the-floor", str(worktree)])
    wc = GitWorkingCopy(git=git)
    config = Config(repo="owner/repo", tech_lead_review_agent=TECH_LEAD)
    ledger = SqliteIssueRunLedger(state / "runs.sqlite", repo_slug="owner/repo")
    run = IssueRunAllocationService(FileSystemSessionOutput(), ledger, wc, configuration=config).allocate(
        IssueRunAllocation(worktree, "coding-1", ISSUE,
            # The launch stamps the kind from the agent role (#7347).
            SessionKey(GitHubIssueKey("owner/repo", str(ISSUE)),
                       SessionKind.for_issue_launch(agent_label, TECH_LEAD)), agent_label, "test",
            terminal_id=f"issue-{ISSUE}"))
    validator = ConfiguredCompletionEvidenceValidator(wc, LocalCommandRunner(),
        IsolatedCompletionValidationWorkspace(state, git, lambda _path: None), command="true", timeout_seconds=30)
    intake = CompletionEvidenceIntakeService(ledger, validator, Mock(spec=HistoricalIntakeHandler),
                                             Mock(spec=BackgroundJobRunner))
    escrow = FilesystemValidatedWorkEscrow(state / "validated-work", repository=repo, repo_slug="owner/repo", git=wc)
    store = SqliteValidatedWorkStore(state / "work.sqlite",
        ancestry=GitValidatedWorkAncestry(repository=repo, repo_slug="owner/repo", git=wc),
        artifacts=escrow, liveness=Liveness(), retention=escrow)
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    labels = Labels()
    aggregate = AggregateRecoveryBlocks(repo_slug="owner/repo", records=store,
        admission=RankedEvidenceAdmission(store, ledger), phases=store, authority=effects,
        gate=FileIssueDispositionMutationGate(state), labels=LabelManager(Config(repo="owner/repo")),
        reader=labels, applier=labels, human_block=NO_OTHER_NEEDS_HUMAN_CAUSES)
    observer = Mock(spec=ValidatedWorkCaptureObserver)
    observer.observe.return_value = ValidatedWorkRemoteFacts(None, ())
    preservation = ValidatedWorkPreservationService(intake=intake, store=aggregate,
        custody=ValidatedWorkCustody(escrow, aggregate), repair=EscrowReconciliation(escrow=escrow, store=aggregate, intake=ledger),
        working_copy=wc, observer=observer,
        # No remote in this rig: the base is unreadable, so the kind alone
        # decides here (the ahead-of-base rule has its own tests).
        base_branch=lambda _issue, _worktree: "main")
    sessions = Mock()
    sessions.exists.return_value = False
    jobs = Mock()
    jobs.cancel_matching.return_value = ()
    lifecycle = IssueRuntimeLifecycleOwners(CoreIssueRuntimeOwners(sessions, [], Mock(), jobs, Mock(), Mock()),
        preservation, IssueRunEvidenceService(ledger, live_runs=lambda issue: (), now=lambda: "2026-09-26T04:24:41Z"),
        Mock(), PublishedReviewCustody(preservation, no_open_pull_requests()))
    events = InMemoryEventSink()
    retirement = OutOfScopeRecordRetirement(intake=ledger, store=store, effects=effects,
                                            blocks=aggregate, events=events)
    rig = SimpleNamespace(git=git, worktree=worktree, ledger=ledger, run=run, intake=intake, store=store,
        execution=execution, labels=labels, lifecycle=lifecycle, events=events, retirement=retirement,
        aggregate=aggregate,
        agent_label=agent_label, state=state)
    receipt = intake.submit(ledger.submission_capability(run), command(completion(), "validated"))
    intake.drain()
    rig.receipt = receipt
    return rig


@pytest.fixture
def tech_lead(tmp_path):
    return _rig(tmp_path, TECH_LEAD)


@pytest.fixture
def coder(tmp_path):
    return _rig(tmp_path, CODER)


def _complete(rig, make_session):
    """Drive the session's end through `handle_session_completion` itself."""
    session = replace(make_session(issue_number=ISSUE, worktree_path=rig.worktree,
                                   branch_name=f"{ISSUE}-health-review-walk-the-floor"),
                      run_assets=rig.run)
    handler = MagicMock()
    handler.process_completion.return_value = SimpleNamespace(
        actions=[], history_status=SessionStatus.COMPLETED,
        history_entry=SessionHistoryEntry(issue_number=ISSUE, title="Health Review", agent_type=rig.agent_label,
                                          status="completed", runtime_minutes=1, pr_url=None),
        pr_url=None, pr_number=None, cleanup=CleanupDecision.immediate(), should_queue_review=False,
    )
    applier = MagicMock()
    applier.runtime_lifecycle = rig.lifecycle
    session_output = MagicMock()
    session_output.attach_claude_log.return_value = None
    handle_session_completion(
        session=session, status=SessionStatus.COMPLETED, state=OrchestratorState(),
        completion_handler=handler, action_applier=applier, observer=MagicMock(), worktree_manager=None,
        kill_session_fn=lambda _terminal_id: None, config=MagicMock(), session_output=session_output,
        pending_work_claims=SqlitePendingWorkClaimStore.for_repo(rig.state),
        processing_policy=CompletionProcessingPolicy.for_unprocessed_session(rig.agent_label, TECH_LEAD),
    )
    return handler.process_completion.call_args.kwargs["recovery_holds_validated_work"]


# -- the rule ---------------------------------------------------------------


@pytest.mark.parametrize("task", list(SessionKind))
def test_only_coding_rework_and_historical_runs_produce_recoverable_work(task):
    label = (
        TECH_LEAD if task is SessionKind.TECH_LEAD
        else HISTORICAL_AGENT_LABEL if task is SessionKind.HISTORICAL
        else CODER
    )
    role = CompletionRunRole(ISSUE, task, label)
    recoverable = {SessionKind.CODE, SessionKind.REWORK, SessionKind.HISTORICAL}
    assert recovery_owns(role) is (task in recoverable)
    if recovery_owns(role):
        with pytest.raises(ValueError):
            outside_scope_reason(role)
    else:
        assert task.value in outside_scope_reason(role)
    # The capability table is the rule's single source (#7347).
    assert recovery_owns(role) is task.capabilities.capturable


# -- capture: new completions ------------------------------------------------


def test_tech_lead_completion_with_validation_adds_no_recovery_pending(tech_lead, make_session):
    """porchpin#410 / exam Case B: the tech lead's own validated completion."""
    holds = _complete(tech_lead, make_session)

    assert holds is False
    assert not tech_lead.store.for_issue(ISSUE).found_work
    assert tech_lead.labels.operations == []
    assert RECOVERY_PENDING not in tech_lead.labels.labels


def test_coding_completion_with_validation_still_hands_recovery_its_work(coder, make_session):
    holds = _complete(coder, make_session)

    assert holds is True
    assert coder.store.for_issue(ISSUE).unresolved
    assert coder.labels.operations == [("add", RECOVERY_PENDING)]


# -- recovery: records admitted before the rule ----------------------------


def _legacy_capture(rig, monkeypatch):
    """Admit this run's work the way capture did before the rule (#7323)."""
    with monkeypatch.context() as legacy:
        legacy.setattr(validated_work_preservation, "recovery_owns", lambda role: True)
        assert rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=rig.run)
    assert rig.labels.labels == {RECOVERY_PENDING}
    (disposition,) = rig.store.for_issue(ISSUE).dispositions
    return disposition


def _strand_publishing(rig, disposition):
    """Leave the record exactly as porchpin#410's: PUBLISHING after a transient attempt."""
    claim = rig.store.acquire_claim(disposition.record_id, expected_states=frozenset({ValidatedWorkState.QUEUED}),
                                    evidence_id=disposition.evidence_id)
    attempt = rig.store.begin_publish_attempt(claim, expected_attempt_no=0,
        target_head_sha=disposition.key.validated_head_sha, expected_remote_head="",
        phase=DispositionPhase.PRE_SUBMISSION, started_at="2026-09-26T04:26:28+00:00")
    assert attempt is not None
    assert rig.store.record_attempt_outcome(claim, attempt, outcome=PublishValidatedHeadStatus.TRANSIENT_FAILURE,
        failure=ValidatedWorkFailure.REMOTE_UNREADABLE, finished_at="2026-09-26T04:30:23+00:00")
    assert rig.store.relinquish_claim(claim)
    assert rig.store.get(disposition.record_id).state is ValidatedWorkState.PUBLISHING


def _operation(rig):
    """The drain's per-record operation; nothing past the scope check may run."""
    preparation = Mock(spec=ClaimedRecoveryPreparation)
    preparation.prepare.return_value = RecoveryAttemptPending("preparation reached")
    return preparation, RecoveryRecordOperation(execution=rig.execution, store=rig.store, preparation=preparation,
        publication=Mock(spec=RecoveryPublicationAttempt), completion=Mock(spec=RecoveryPublicationCompletion),
        scope=rig.retirement)


@pytest.mark.parametrize("stranded_in", ["queued", "publishing"])
def test_existing_tech_lead_record_is_retired_and_releases_recovery_pending(tech_lead, monkeypatch, stranded_in):
    disposition = _legacy_capture(tech_lead, monkeypatch)
    if stranded_in == "publishing":
        _strand_publishing(tech_lead, disposition)
    preparation, operation = _operation(tech_lead)

    result = operation.run(RecoveryRecordRequest(disposition.record_id, disposition.evidence_id), OrchestratorState())

    assert isinstance(result, RecoveryAttemptPending)
    assert "outside recovery scope" in result.message
    preparation.prepare.assert_not_called()
    record = tech_lead.store.record_for_id(disposition.record_id)
    assert record.disposition.state is ValidatedWorkState.ABANDONED
    assert record.resolution_kind is ResolutionKind.OUTSIDE_RECOVERY_SCOPE
    assert record.disposition.resolution is not None
    assert record.disposition.resolution.actor == RETIREMENT_ACTOR
    assert "tech-lead run" in record.disposition.resolution.reason
    assert tech_lead.store.owner_of(disposition.record_id) is None
    assert not tech_lead.store.has_unresolved_work(ISSUE)
    assert tech_lead.labels.labels == set()
    assert tech_lead.labels.operations[-1] == ("remove", RECOVERY_PENDING)
    event = tech_lead.events.last_event(EventName.VALIDATED_WORK_ABANDONED.value)
    assert event.data["resolution_kind"] == ResolutionKind.OUTSIDE_RECOVERY_SCOPE.value
    assert event.data["record_id"] == disposition.record_id
    # Resolved: the drain's next pass leaves it alone.
    assert operation.run(RecoveryRecordRequest(disposition.record_id, disposition.evidence_id),
                         OrchestratorState()).message == "Retained work is not authorized for publication"


def test_existing_coding_record_is_left_to_recovery(coder, monkeypatch):
    disposition = _legacy_capture(coder, monkeypatch)
    preparation, operation = _operation(coder)

    result = operation.run(RecoveryRecordRequest(disposition.record_id, disposition.evidence_id), OrchestratorState())

    assert result == RecoveryAttemptPending("preparation reached")
    preparation.prepare.assert_called_once()
    assert coder.store.get(disposition.record_id).state is ValidatedWorkState.QUEUED
    assert coder.labels.labels == {RECOVERY_PENDING}
    assert coder.events.get_events(EventName.VALIDATED_WORK_ABANDONED.value) == []


def test_retirement_without_exact_custody_proof_resolves_nothing(tech_lead, monkeypatch):
    """No proof of the run's role, no retirement: the failure is loud, the block stays."""
    disposition = _legacy_capture(tech_lead, monkeypatch)
    preparation, operation = _operation(tech_lead)
    monkeypatch.setattr(tech_lead.ledger, "prepare_evidence",
                        Mock(side_effect=CompletionIntakeError("retained evidence has no exact owner proof")))

    with pytest.raises(CompletionIntakeError):
        operation.run(RecoveryRecordRequest(disposition.record_id, disposition.evidence_id), OrchestratorState())

    assert tech_lead.store.get(disposition.record_id).state is ValidatedWorkState.QUEUED
    assert tech_lead.store.owner_of(disposition.record_id) is None
    assert tech_lead.labels.labels == {RECOVERY_PENDING}


def test_store_retires_only_the_exact_evidence_set_under_a_live_claim(tech_lead, monkeypatch):
    disposition = _legacy_capture(tech_lead, monkeypatch)
    store = tech_lead.store
    claim = store.acquire_claim(disposition.record_id, expected_states=frozenset({ValidatedWorkState.QUEUED}),
                                evidence_id=disposition.evidence_id)
    exact = frozenset({disposition.evidence_id})

    assert not store.retire_outside_scope(claim, evidence_ids=exact | {"e1:unproven"}, actor="a", reason="r")
    assert not store.retire_outside_scope(claim, evidence_ids=frozenset(), actor="a", reason="r")
    assert store.get(disposition.record_id).state is ValidatedWorkState.QUEUED
    assert store.relinquish_claim(claim)
    assert not store.retire_outside_scope(claim, evidence_ids=exact, actor="a", reason="r")
    assert store.get(disposition.record_id).state is ValidatedWorkState.QUEUED

    live = store.acquire_claim(disposition.record_id, expected_states=frozenset({ValidatedWorkState.QUEUED}),
                               evidence_id=disposition.evidence_id)
    assert store.retire_outside_scope(live, evidence_ids=exact, actor="a", reason="r")
    assert not store.retire_outside_scope(live, evidence_ids=exact, actor="a", reason="r")
    assert store.get(disposition.record_id).state is ValidatedWorkState.ABANDONED


# -- the other admission / drain paths --------------------------------------


def test_escrow_repair_never_re_admits_an_out_of_scope_orphan(tech_lead, monkeypatch):
    """A pre-rule tech-lead capture that crashed after escrow, before the store row."""
    with monkeypatch.context() as legacy:
        legacy.setattr(validated_work_preservation, "recovery_owns", lambda role: True)
        legacy.setattr(tech_lead.aggregate, "admit", Mock(side_effect=OSError("crash before admission")))
        assert not tech_lead.lifecycle.preserve_completed_run(
            ISSUE, f"issue-{ISSUE}", "session-completion", run=tech_lead.run)
    assert not tech_lead.store.for_issue(ISSUE).found_work

    # The next terminal boundary runs escrow repair first.
    assert not tech_lead.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=tech_lead.run)

    assert not tech_lead.store.for_issue(ISSUE).found_work
    assert tech_lead.labels.operations == []


def test_escrow_repair_still_re_admits_a_coding_orphan(coder, monkeypatch):
    with monkeypatch.context() as crash:
        crash.setattr(coder.aggregate, "admit", Mock(side_effect=OSError("crash before admission")))
        assert not coder.lifecycle.preserve_completed_run(
            ISSUE, f"issue-{ISSUE}", "session-completion", run=coder.run)

    assert coder.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coder.run)
    assert coder.store.for_issue(ISSUE).unresolved
    assert coder.labels.labels == {RECOVERY_PENDING}


class _UnreadableRemote:
    def __init__(self) -> None:
        self.reads = 0

    def observe(self, request):
        self.reads += 1
        raise PublicationRemoteError("remote unreadable")


@pytest.mark.parametrize("task", [SessionKind.TECH_LEAD, SessionKind.CODE])
def test_drain_retires_an_unobserved_parked_tech_lead_record_before_any_remote_read(tmp_path, task):
    """The remote-authority refresh lane reaches retirement too (PARKED, remote_unreadable)."""
    store = Rig(tmp_path / "work.sqlite").open()
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    labels = Labels(issue=6914)
    intake = owned_intake(task)
    aggregate = AggregateRecoveryBlocks(repo_slug="owner/repo", records=store,
        admission=RankedEvidenceAdmission(store, intake), phases=store, authority=effects,
        gate=FileIssueDispositionMutationGate(tmp_path), labels=LabelManager(Config(repo="owner/repo")),
        reader=labels, applier=labels, human_block=NO_OTHER_NEEDS_HUMAN_CAUSES)
    admission = capture(state=ValidatedWorkState.PARKED, failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
                        reason="capture read failed", remote_status=RemoteBaselineStatus.UNOBSERVED)
    aggregate.admit(admission)
    assert labels.labels == {RECOVERY_PENDING}
    events = InMemoryEventSink()
    scope = OutOfScopeRecordRetirement(intake=intake, store=store, effects=effects, blocks=aggregate, events=events)
    remote = _UnreadableRemote()
    drain = RecoveryDrain(
        queue=store,
        operation=RecoveryRecordOperation(execution=execution, store=store, preparation=Mock(),
                                          publication=Mock(), completion=Mock(), scope=scope),
        authority_refresh=RemoteAuthorityRefreshOperation(execution=execution, effects=effects, store=store,
                                                          observer=remote, scope=scope),
        claim_maintenance=NullRetainedClaimMaintenance(), block_sweep=NullRecoveryBlockSweep(),
        scope_sweep=NullValidatedWorkScopeSweep(),  # the refresh lane alone must retire it
        batch_size=5, interval_seconds=1,
        liveness=drain_liveness(),
    )

    drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    record = store.record_for_id(admission.evidence.record_id)
    if task is SessionKind.TECH_LEAD:
        assert record.disposition.state is ValidatedWorkState.ABANDONED
        assert record.resolution_kind is ResolutionKind.OUTSIDE_RECOVERY_SCOPE
        assert remote.reads == 0
        assert labels.labels == set()
    else:
        assert record.disposition.state is ValidatedWorkState.PARKED
        assert remote.reads == 1
        assert labels.labels == {RECOVERY_PENDING}


@pytest.mark.parametrize("task", [SessionKind.TECH_LEAD, SessionKind.CODE])
@pytest.mark.parametrize("state, failure", [
    (ValidatedWorkState.PARKED, ValidatedWorkFailure.PR_BRANCH_MISMATCH),
    (ValidatedWorkState.FAILED, ValidatedWorkFailure.ARTIFACT_MISSING),
])
def test_drain_scope_sweep_retires_records_no_publication_lane_selects(tmp_path, task, state, failure):
    """PARKED-with-observed-authority and FAILED records are drained by neither lane."""
    store = Rig(tmp_path / "work.sqlite").open()
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    labels = Labels(issue=6914)
    intake = owned_intake(task)
    human = NeedsHumanBlock("needs-human", "tech-lead-needs-human", labels, labels.read_issue_labels,
                            frozenset, SqlitePendingWorkClaimStore(tmp_path / "causes.sqlite"))
    aggregate = AggregateRecoveryBlocks(repo_slug="owner/repo", records=store,
        admission=RankedEvidenceAdmission(store, intake), phases=store, authority=effects,
        gate=FileIssueDispositionMutationGate(tmp_path), labels=LabelManager(Config(repo="owner/repo")),
        reader=labels, applier=labels, human_block=human)
    admission = capture(state=state, failure=failure, reason="admitted before the scope rule")
    aggregate.admit(admission)
    assert store.drain_requests(after_record_id="", limit=10) == ()
    assert RECOVERY_PENDING in labels.labels
    assert ("needs-human" in labels.labels) is (state is ValidatedWorkState.FAILED)
    fence = store.record_for_id(admission.evidence.record_id).owner_fence
    retirement = OutOfScopeRecordRetirement(intake=intake, store=store, effects=effects,
                                            blocks=aggregate, events=InMemoryEventSink())
    drain = RecoveryDrain(
        queue=store, operation=Mock(), authority_refresh=Mock(),
        claim_maintenance=NullRetainedClaimMaintenance(), block_sweep=NullRecoveryBlockSweep(),
        scope_sweep=OutOfScopeRetirementSweep(source=store, store=store, execution=execution,
                                              retirement=retirement, batch_size=5,
                                              liveness=drain_liveness()),
        batch_size=5, interval_seconds=1,
        liveness=drain_liveness(),
    )

    report = drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    record = store.record_for_id(admission.evidence.record_id)
    assert store.owner_of(admission.evidence.record_id) is None
    if task is SessionKind.TECH_LEAD:
        assert report.scope_sweep.retired == (admission.evidence.record_id,)
        assert record.disposition.state is ValidatedWorkState.ABANDONED
        assert record.resolution_kind is ResolutionKind.OUTSIDE_RECOVERY_SCOPE
        assert RECOVERY_PENDING not in labels.labels
        assert "needs-human" not in labels.labels
    else:
        assert report.scope_sweep.retired == ()
        assert record.disposition.state is state
        assert record.owner_fence == fence  # proven in scope without ever being claimed
        assert RECOVERY_PENDING in labels.labels
        # Proven once: the next tick does not re-read custody for the same evidence.
        calls = intake.prepare_evidence.call_count
        drain._next_at = float("-inf")
        drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)
        assert intake.prepare_evidence.call_count == calls


def test_scope_sweep_reports_nothing_when_newer_evidence_lands_before_its_cas(tmp_path):
    """The store refuses the retirement; the sweep must not report it retired."""
    store = Rig(tmp_path / "work.sqlite").open()
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    labels = Labels(issue=6914)
    intake = owned_intake(SessionKind.TECH_LEAD)
    aggregate = AggregateRecoveryBlocks(repo_slug="owner/repo", records=store,
        admission=RankedEvidenceAdmission(store, intake), phases=store, authority=effects,
        gate=FileIssueDispositionMutationGate(tmp_path), labels=LabelManager(Config(repo="owner/repo")),
        reader=labels, applier=labels, human_block=NO_OTHER_NEEDS_HUMAN_CAUSES)
    admission = capture(state=ValidatedWorkState.PARKED, failure=ValidatedWorkFailure.PR_BRANCH_MISMATCH,
                        reason="admitted before the scope rule")
    aggregate.admit(admission)
    events = InMemoryEventSink()
    retirement = OutOfScopeRecordRetirement(intake=intake, store=store, effects=effects,
                                            blocks=aggregate, events=events)
    real_cas = store.retire_outside_scope

    def evidence_lands_first(claim, **kwargs):
        store.admit(capture(state=ValidatedWorkState.PARKED, failure=ValidatedWorkFailure.PR_BRANCH_MISMATCH,
                            reason="newer run on the same head", run="run-2"))
        return real_cas(claim, **kwargs)

    store.retire_outside_scope = evidence_lands_first
    sweep = OutOfScopeRetirementSweep(source=store, store=store, execution=execution,
                                      retirement=retirement, batch_size=5,
                                      liveness=drain_liveness())

    report = sweep.tick(lambda: RecoveryDrainMode.ACTIVE)

    assert report.retired == ()
    assert store.record_for_id(admission.evidence.record_id).disposition.state is ValidatedWorkState.PARKED
    assert store.owner_of(admission.evidence.record_id) is None
    assert RECOVERY_PENDING in labels.labels
    assert events.get_events(EventName.VALIDATED_WORK_ABANDONED.value) == []


class _SwitchableExecution:
    """The real execution owner, held by another owner while ``busy``."""

    def __init__(self, real) -> None:
        self._real, self.busy = real, False

    def try_enter(self, record_id):
        from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy

        return RecordExecutionBusy(record_id) if self.busy else self._real.try_enter(record_id)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _scope_rig(tmp_path, proof, *, issues=(6914,)):
    """A real sweep over a real store with one parked record and a real owner;
    the scope proof is *proof*, recorded in ``rig.proofs``."""
    from datetime import timedelta
    from types import SimpleNamespace

    from issue_orchestrator.domain.action_liveness import LivenessPolicy
    from issue_orchestrator.ports.recovery_block import RecoveryBlockIssueReconciler
    from tests.unit.control.liveness_doubles import (
        InMemoryActionLivenessStore,
        ManualClock,
        RecordingEscalation,
        liveness_owner,
    )

    store = Rig(tmp_path / "work.sqlite").open()
    execution = _SwitchableExecution(LocalValidatedWorkExecutionOwner(store))
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    admissions = [
        capture(state=ValidatedWorkState.PARKED, failure=ValidatedWorkFailure.PR_BRANCH_MISMATCH,
                reason="admitted before the scope rule", issue=issue)
        for issue in issues
    ]
    for admission in admissions:
        store.admit(admission)
    retirement = OutOfScopeRecordRetirement(
        intake=owned_intake(SessionKind.TECH_LEAD), store=store, effects=effects,
        blocks=Mock(spec=RecoveryBlockIssueReconciler), events=InMemoryEventSink(),
    )
    rig = SimpleNamespace(
        store=store, execution=execution, record_id=admissions[0].evidence.record_id,
        record_ids=[admission.evidence.record_id for admission in admissions],
        proofs=[], attached=frozenset(), clock=ManualClock(), escalation=RecordingEscalation(),
        rows=InMemoryActionLivenessStore(), policy=LivenessPolicy(max_attempts=3),
    )

    def recorded_proof(record):
        rig.proofs.append(record.disposition.record_id)
        return proof(record)

    retirement.recovery_owns_record = recorded_proof
    rig.attached_evidence = lambda _rid: rig.attached
    owner = liveness_owner(store=rig.rows, escalation=rig.escalation, clock=rig.clock,
                           policy=rig.policy)
    rig.sweep = OutOfScopeRetirementSweep(
        source=store, store=store, execution=execution, retirement=retirement, batch_size=5,
        liveness=drain_liveness(
            owner,
            records=SimpleNamespace(
                get=store.get,
                # The full read the key must NOT depend on (review B r7).
                record_for_id=lambda rid: store.record_for_id(rid),
                attached_evidence=lambda rid: tuple(
                    SimpleNamespace(evidence_id=evidence_id)
                    for evidence_id in sorted(rig.attached_evidence(rid))
                ),
            ),
        ),
    )

    def passes(count, step=timedelta(hours=1)):
        for _ in range(count):
            rig.sweep.tick(lambda: RecoveryDrainMode.ACTIVE)
            rig.clock.advance(step)

    rig.passes = passes
    return rig


def _unprovable(record):
    raise RuntimeError("intake ledger unreadable")


def test_a_scope_judgement_held_by_another_owner_is_a_visible_wait(tmp_path):
    """A record no publication lane selects, whose lease stays held: shown as
    a zero-attempt wait, never parked, and cleared once judged (review B r3)."""
    rig = _scope_rig(tmp_path, proof=lambda record: True)
    rig.execution.busy = True

    rig.passes(10)

    assert rig.proofs == []
    [row] = rig.rows.waiting_rows()
    assert row.key.identity.action == "judge_record_scope"
    assert row.key.escalation_issue == rig.store.record_for_id(rig.record_id).disposition.key.issue_number
    assert row.attempts == 0 and "another owner" in row.last_reason
    assert rig.escalation.parked == []

    rig.execution.busy = False
    rig.passes(1)

    assert rig.proofs == [rig.record_id]
    assert rig.rows.rows == {}


def test_newly_attached_evidence_is_a_new_scope_question(tmp_path):
    """The judgement reads attached evidence too; a parked judgement is asked
    again once new evidence attaches (review B r3)."""
    rig = _scope_rig(tmp_path, proof=_unprovable)
    rig.passes(10)
    assert len(rig.proofs) == rig.policy.max_attempts

    rig.attached = frozenset({"e-attached"})
    rig.passes(1)

    assert len(rig.proofs) == rig.policy.max_attempts + 1


def test_a_retirement_the_store_keeps_refusing_is_bounded(tmp_path):
    """The store refuses the retirement (CHANGED) every pass while the record's
    facts stay put: a loop, so it spends a budget and parks (review B r4)."""
    rig = _scope_rig(tmp_path, proof=lambda record: False)
    refusals: list[str] = []

    def refuse(claim, **kwargs):
        refusals.append(claim.record_id)
        return False

    rig.store.retire_outside_scope = refuse

    rig.passes(30)

    assert len(refusals) == rig.policy.max_attempts
    [parked] = rig.escalation.parked
    assert parked.key.identity.action == "judge_record_scope"
    assert "refused" in parked.last_reason


def test_unreadable_attached_evidence_is_bounded_and_never_starves_the_sweep(tmp_path):
    """The first record's attached evidence cannot be read, by the key and by
    the proof alike. The READ is attempted exactly max_attempts times, then
    the record parks and reads stop; the second record is still judged every
    pass; after a release, a read that succeeds is a new question (review B
    r5, r8, r10)."""
    from datetime import timedelta

    unreadable: set[str] = set()
    reads: list[str] = []

    def attached_evidence(record_id):
        if record_id in unreadable:
            reads.append(record_id)
            raise OSError("evidence store unreadable")
        return frozenset()

    def proof(record):
        attached_evidence(record.disposition.record_id)
        return True

    rig = _scope_rig(tmp_path, proof=proof, issues=(6914, 6915))
    rig.attached_evidence = attached_evidence
    first, second = rig.record_ids
    unreadable.add(first)
    minute = timedelta(minutes=1)
    for _ in range(30):
        # A proof that succeeds is cached per evidence; forget it each pass so
        # the second record keeps being judged.
        rig.sweep._owned.clear()
        rig.passes(1, step=minute)

    assert reads.count(first) == rig.policy.max_attempts
    assert rig.proofs.count(second) == 30
    [parked] = rig.escalation.parked
    assert parked.key.identity.subject == f"validated_work:{first}"
    assert "evidence store unreadable" in parked.last_reason

    # Parked: no further reads, however long it stays unreadable.
    rig.passes(10, step=rig.policy.max_backoff)
    assert reads.count(first) == rig.policy.max_attempts

    # Healed, and an operator releases the park: read once more, and the
    # readable set is a new question, judged at once.
    unreadable.clear()
    from issue_orchestrator.domain.action_liveness import ActionIdentity

    rig.sweep._liveness.owner.release_identity(
        ActionIdentity(f"validated_work:{first}", "judge_record_scope")
    )
    judged = rig.proofs.count(first)
    rig.passes(1, step=minute)
    assert rig.proofs.count(first) == judged + 1


def test_an_unreadable_record_is_judged_bounded_and_never_starves_the_sweep(tmp_path):
    """The first record's disposition reads but its full record does not. The
    key needs only the disposition, so the failing read happens inside the
    judgement: bounded and parked, while the second record keeps being judged
    (review B r7)."""
    rig = _scope_rig(tmp_path, proof=lambda record: True, issues=(6914, 6915))
    first, second = rig.record_ids
    before = rig.store.get(first)
    full_read = rig.store.record_for_id
    reads: list[str] = []

    def record_for_id(record_id):
        if record_id == first:
            reads.append(record_id)
            raise ValueError("evidence payload does not decode")
        return full_read(record_id)

    rig.store.record_for_id = record_for_id
    for _ in range(10):
        rig.sweep._owned.clear()
        rig.passes(1)

    assert len(reads) == rig.policy.max_attempts
    assert rig.proofs.count(second) == 10
    [parked] = rig.escalation.parked
    assert parked.key.identity.subject == f"validated_work:{first}"
    assert parked.key.escalation_issue == before.key.issue_number
    assert rig.store.get(first) == before


def test_retiring_a_record_releases_every_lanes_park(tmp_path):
    """Recovery parked the record; the scope sweep's retirement is refused
    once, then succeeds. The record is resolved, so the recovery park goes
    too and its block is withdrawn, not left for days (review B r9)."""
    from issue_orchestrator.domain.action_liveness import ActionOutcome
    from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest

    rig = _scope_rig(tmp_path, proof=lambda record: False)
    record = rig.store.record_for_id(rig.record_id)
    liveness = rig.sweep._liveness
    recover_key = liveness.key(
        RecoveryRecordRequest(rig.record_id, record.current_evidence.evidence_id)
    )
    liveness.owner.record(recover_key, ActionOutcome.permanent("publish refused"))
    [parked] = rig.escalation.parked
    assert parked.key == recover_key
    real_retire = rig.store.retire_outside_scope
    attempts: list[str] = []

    def refuse_once(claim, **kwargs):
        attempts.append(claim.record_id)
        return False if len(attempts) == 1 else real_retire(claim, **kwargs)

    rig.store.retire_outside_scope = refuse_once
    rig.passes(3)

    assert len(attempts) == 2
    assert rig.store.get(rig.record_id).state is ValidatedWorkState.ABANDONED
    assert rig.rows.rows == {}
    rig.clock.advance(rig.policy.max_backoff)
    liveness.owner.reconcile_effects()
    assert recover_key in [row.key for batch in rig.escalation.released for row in batch]
    assert rig.escalation.unblocks == [(recover_key.escalation_issue, True)]


def test_a_retirement_that_committed_before_a_later_step_raised_still_resolves(tmp_path):
    """The store retires the record, then the block projection raises. The
    record is resolved, so every lane's park is released rather than the
    error being counted against a record the sweep will never see again
    (review B r10)."""
    from issue_orchestrator.domain.action_liveness import ActionOutcome
    from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest

    rig = _scope_rig(tmp_path, proof=lambda record: False)
    record = rig.store.record_for_id(rig.record_id)
    liveness = rig.sweep._liveness
    recover_key = liveness.key(
        RecoveryRecordRequest(rig.record_id, record.current_evidence.evidence_id)
    )
    liveness.owner.record(recover_key, ActionOutcome.permanent("publish refused"))
    rig.sweep._retirement._blocks.reconcile_issue_block.side_effect = RuntimeError(
        "label projection failed"
    )

    report = rig.sweep.tick(lambda: RecoveryDrainMode.ACTIVE)

    assert rig.store.get(rig.record_id).state is ValidatedWorkState.ABANDONED
    assert report.retired == (rig.record_id,)
    assert rig.rows.rows == {}
    rig.clock.advance(rig.policy.max_backoff)
    liveness.owner.reconcile_effects()
    assert rig.escalation.unblocks == [(recover_key.escalation_issue, True)]


def test_a_scope_judgement_that_raises_every_pass_is_bounded(tmp_path):
    """The scope sweep re-selects an unchanged record each interval. A proof
    that raises every time is held, then parked on the record's issue, and the
    retained work is untouched (#7350 review B r2)."""
    rig = _scope_rig(tmp_path, proof=_unprovable)
    before = rig.store.record_for_id(rig.record_id)

    rig.passes(30)

    assert len(rig.proofs) == rig.policy.max_attempts
    [parked] = rig.escalation.parked
    assert parked.key.identity.action == "judge_record_scope"
    assert parked.key.escalation_issue == before.disposition.key.issue_number
    assert "intake ledger unreadable" in parked.last_reason
    assert rig.store.record_for_id(rig.record_id) == before
