"""Work its own completion already published is recorded as published (porchpin #186).

The porchpin 2026-09-28 sequence, on real Git (a bare ``origin``), SQLite,
intake, escrow and the aggregate recovery block; only GitHub's PR listing and
the issue's labels are fakes, and the PR listing reads the real bare remote.

1. Recovery published the issue's first validated head ``W1`` into PR #381
   (the lineage's durable published head).
2. Review sent the PR back. The rework run rebased onto a moved ``main`` and
   validated ``V1``; its review exchange committed ``V2`` on top and the
   completion's own push force-updated the PR branch to ``V2``.
3. Every terminal boundary then captured those validated heads. ``V1``/``V2``
   diverge from ``W1`` (the rebase), so each record parked
   ``divergent_validated_heads`` and ``recovery-pending`` went back on an issue
   whose work was already published and under review.

The right answer: the open PR's head ``V2`` is the lineage's published head
(the completion's push is a publication route of its own), so the captures
resolve as contained in it, and nothing blocks the issue.
"""

import itertools
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.adapters.issue_disposition_gate import FileIssueDispositionMutationGate
from issue_orchestrator.control import validated_work_preservation
from issue_orchestrator.control.aggregate_recovery_block import AggregateRecoveryBlocks
from issue_orchestrator.control.completion_handler import CleanupDecision, SessionStatus
from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import ConfiguredCompletionEvidenceValidator
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.control.issue_run_evidence import IssueRunEvidenceService
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
from issue_orchestrator.control.published_review_custody import PublishedReviewCustody
from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.control.review_exchange_lifecycle import (
    CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners,
)
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission
from issue_orchestrator.control.validated_work_capture import ValidatedWorkCustody
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.control.validated_work_escrow import EscrowReconciliation
from issue_orchestrator.control.validated_work_preservation import ValidatedWorkPreservationService
from issue_orchestrator.control.validated_work_published_head import (
    PullRequestCarriage, PullRequestPublicationRecorder,
)
from issue_orchestrator.control.validated_work_scope_retirement import (
    OutOfScopeRecordRetirement, OutOfScopeRetirementSweep,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.models import OrchestratorState, SessionHistoryEntry
from issue_orchestrator.domain.publication_remote import (
    PublicationPrState, PublicationPullRequest, PublicationRemoteError,
)
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.registered_completion import CompletionProcessingPolicy
from issue_orchestrator.domain.session_key import SessionKey
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.validated_work import (
    DispositionPhase, LineageRole, PublicationProvenance, ResolutionKind, ValidatedWorkFailure,
    ValidatedWorkKey, ValidatedWorkState, canonical_lineage_key,
)
from issue_orchestrator.domain.validated_work_capture import ValidatedWorkRemoteFacts
from issue_orchestrator.domain.validated_work_remote_authority import (
    LandedViaMergedPullRequest, PublishedOnOpenPullRequest, carried_by_open_pull_request,
    landed_via_merged_pull_request,
)
from issue_orchestrator.domain.validated_work_store import AncestryRelation, PrPublicationStatus
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
from tests.runtime_lifecycle_helpers import no_open_pull_requests
from tests.unit.control.liveness_doubles import drain_liveness
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_validated_work_scope import Labels
from tests.unit.validated_work_support import Liveness

ISSUE = 186
PR = 381
BRANCH = "186-cuj-h4-exhaustive-pair-phone-contested-no-show-jou"
REPO = "owner/repo"
CODER = "agent:coder"
RECOVERY_PENDING = "recovery-pending"


class GitHubPulls:
    """GitHub's branch/PR view, read from the real bare remote.

    The branch head is whatever ``origin`` holds now; PR #381 is open on it
    unless a test closes it. ``reads`` counts the uncached observations.
    """

    def __init__(self, git, origin: Path) -> None:
        self._git, self._origin = git, origin
        self.open = True
        self.unreadable = False
        self.reads = 0
        # Set once PR #381 is merged: GitHub then lists it as merged, its
        # head frozen at ``refs/pull/381/head``.
        self.merged = False

    def merged_pull_requests(self, request):
        self.reads += 1
        if self.unreadable:
            raise PublicationRemoteError("GitHub is unreadable")
        assert (request.repo_slug, request.issue_number, request.branch_name) == (REPO, ISSUE, BRANCH)
        if not self.merged:
            return ()
        head = self._git.run(self._origin, ["rev-parse", f"refs/pull/{PR}/head"]).stdout.strip()
        return (PublicationPullRequest(
            PR, f"https://github.com/{REPO}/pull/{PR}", REPO, REPO, BRANCH, "main", head,
            PublicationPrState.MERGED, "Closes #186",
        ),)

    def observe(self, request):
        self.reads += 1
        if self.unreadable:
            raise PublicationRemoteError("GitHub is unreadable")
        assert (request.repo_slug, request.issue_number, request.branch_name) == (REPO, ISSUE, BRANCH)
        listed = self._git.run(self._origin, ["for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}"])
        head = listed.stdout.strip() or None
        if head is None or not self.open or self.merged:
            return ValidatedWorkRemoteFacts(head, ())
        return ValidatedWorkRemoteFacts(head, (PublicationPullRequest(
            PR, f"https://github.com/{REPO}/pull/{PR}", REPO, REPO, BRANCH, "main", head,
            PublicationPrState.OPEN, "Closes #186",
        ),))


def _commit(git, worktree: Path, name: str, text: str) -> str:
    (worktree / name).write_text(text)
    git.run(worktree, ["add", name])
    git.run(worktree, ["commit", "-m", f"#186: {name}"])
    return git.head_sha(worktree)


@pytest.fixture
def rig(tmp_path):
    git = create_git(LocalCommandRunner())
    origin, repo, worktree = tmp_path / "origin.git", tmp_path / "repository", tmp_path / "porchpin-186"
    state = tmp_path / "owner-state"
    git.run(tmp_path, ["init", "--bare", "-b", "main", str(origin)])
    repo.mkdir()
    git.run(repo, ["init", "-b", "main"])
    git.run(repo, ["config", "user.name", "Rework test"])
    git.run(repo, ["config", "user.email", "test@example.invalid"])
    (repo / "content").write_text("base")
    git.run(repo, ["add", "content"])
    git.run(repo, ["commit", "-m", "base"])
    git.run(repo, ["remote", "add", "origin", str(origin)])
    git.run(repo, ["push", "-q", "origin", "main"])
    git.run(repo, ["worktree", "add", "-b", BRANCH, str(worktree)])
    wc = GitWorkingCopy(git=git)
    config = Config(repo=REPO)
    ledger = SqliteIssueRunLedger(state / "runs.sqlite", repo_slug=REPO)
    validator = ConfiguredCompletionEvidenceValidator(wc, LocalCommandRunner(),
        IsolatedCompletionValidationWorkspace(state, git, lambda _path: None), command="true", timeout_seconds=30)
    intake = CompletionEvidenceIntakeService(ledger, validator, Mock(spec=HistoricalIntakeHandler),
                                             Mock(spec=BackgroundJobRunner))
    escrow = FilesystemValidatedWorkEscrow(state / "validated-work", repository=repo, repo_slug=REPO, git=wc)
    store = SqliteValidatedWorkStore(state / "work.sqlite",
        ancestry=GitValidatedWorkAncestry(repository=repo, repo_slug=REPO, git=wc),
        artifacts=escrow, liveness=Liveness(), retention=escrow)
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    labels = Labels(issue=ISSUE)
    aggregate = AggregateRecoveryBlocks(repo_slug=REPO, records=store,
        admission=RankedEvidenceAdmission(store, ledger), phases=store, authority=effects,
        gate=FileIssueDispositionMutationGate(state), labels=LabelManager(config),
        reader=labels, applier=labels,
        human_block=NeedsHumanBlock("needs-human", "tech-lead-needs-human", labels, labels.read_issue_labels,
                                    frozenset, SqlitePendingWorkClaimStore(state / "causes.sqlite")))
    github = GitHubPulls(git, origin)
    preservation = ValidatedWorkPreservationService(intake=intake, store=aggregate,
        custody=ValidatedWorkCustody(escrow, aggregate),
        repair=EscrowReconciliation(escrow=escrow, store=aggregate, intake=ledger),
        working_copy=wc, observer=github, base_branch=lambda _issue, _worktree: "main",
        carriage=PullRequestCarriage(git=wc, observer=github))
    sessions = Mock()
    sessions.exists.return_value = False
    jobs = Mock()
    jobs.cancel_matching.return_value = ()
    lifecycle = IssueRuntimeLifecycleOwners(CoreIssueRuntimeOwners(sessions, [], Mock(), jobs, Mock(), Mock()),
        preservation, IssueRunEvidenceService(ledger, live_runs=lambda issue: (), now=lambda: "2026-09-28T11:46:25Z"),
        Mock(), PublishedReviewCustody(preservation, no_open_pull_requests()))
    events = InMemoryEventSink()
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), ledger, wc, configuration=config)
    rig = SimpleNamespace(git=git, origin=origin, repo=repo, worktree=worktree, state=state, ledger=ledger,
        intake=intake, store=store, execution=execution, effects=effects, labels=labels, aggregate=aggregate,
        github=github, lifecycle=lifecycle, events=events, allocator=allocator, wc=wc)
    rig.retirement = OutOfScopeRecordRetirement(intake=ledger, store=store, effects=effects,
                                                blocks=aggregate, events=events)
    return rig


def _run(rig, kind: SessionKind, session: str, terminal: str):
    return rig.allocator.allocate(IssueRunAllocation(
        rig.worktree, session, ISSUE, SessionKey(GitHubIssueKey(REPO, str(ISSUE)), kind), CODER, "test",
        terminal_id=terminal))


def _validate(rig, run, key: str) -> str:
    """The run's agent completes at the worktree's HEAD, which then validates."""
    receipt = rig.intake.submit(rig.ledger.submission_capability(run), command(completion(), key))
    rig.intake.drain()
    return rig.ledger.validation_for_receipt(receipt.entry_id).head_sha


def _push(rig) -> None:
    """The completion's own push_branch: force-updates the PR branch."""
    rig.git.run(rig.worktree, ["push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}"])


def _recovery_published_first_head(rig) -> str:
    """Step 1: recovery holds and publishes W1; the lineage's published head is W1."""
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    assert _validate(rig, coding, "coding-1") == w1
    # Nothing is on the remote yet (the exchange halted): recovery must hold it.
    assert rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coding)
    (record,) = rig.store.for_issue(ISSUE).dispositions
    _push(rig)
    resolved = rig.store.resolve_observed_merge(
        record_id=record.record_id, merged_head_sha=w1, observed_at="2026-09-23T07:00:47+00:00")
    assert not isinstance(resolved, str), resolved
    assert rig.store.get(record.record_id).state is ValidatedWorkState.RECOVERED
    rig.aggregate.reconcile_issue_block(ISSUE)
    assert RECOVERY_PENDING not in rig.labels.labels
    rig.labels.operations.clear()
    return w1


def _rework_rebases_onto_moved_main(rig) -> None:
    rig.git.run(rig.repo, ["checkout", "-q", "--detach", "main"])
    (rig.repo / "upstream").write_text("another PR merged")
    rig.git.run(rig.repo, ["add", "upstream"])
    rig.git.run(rig.repo, ["commit", "-q", "-m", "another PR merged"])
    rig.git.run(rig.repo, ["push", "-q", "origin", "HEAD:refs/heads/main"])
    rig.git.run(rig.worktree, ["fetch", "-q", "origin", "main"])
    rig.git.run(rig.worktree, ["rebase", "-q", "origin/main"])


def _complete(rig, make_session, run) -> bool:
    """Drive the rework session's end through `handle_session_completion`."""
    session = replace(make_session(issue_number=ISSUE, task=SessionKind.REWORK, worktree_path=rig.worktree,
                                   branch_name=BRANCH), run_assets=run)
    handler = MagicMock()
    handler.process_completion.return_value = SimpleNamespace(
        actions=[], history_status=SessionStatus.COMPLETED,
        history_entry=SessionHistoryEntry(issue_number=ISSUE, title="CUJ H4", agent_type=CODER,
                                          status="completed", runtime_minutes=69,
                                          pr_url=f"https://github.com/{REPO}/pull/{PR}"),
        pr_url=f"https://github.com/{REPO}/pull/{PR}", pr_number=PR, cleanup=CleanupDecision.immediate(),
        should_queue_review=False,
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
        processing_policy=CompletionProcessingPolicy.for_unprocessed_session(CODER, None),
        pr_url_hint=f"https://github.com/{REPO}/pull/{PR}",
    )
    return handler.process_completion.call_args.kwargs["recovery_holds_validated_work"]


def _rework_published_by_its_completion(rig, make_session) -> tuple[str, str, bool]:
    """Steps 2-3 exactly as porchpin #186 ran them; returns (V1, V2, holds)."""
    _rework_rebases_onto_moved_main(rig)
    rework = _run(rig, SessionKind.REWORK, "coding-2", f"rework-{ISSUE}")
    v1 = _validate(rig, rework, "coding-2")
    exchange = _run(rig, SessionKind.REWORK, "review-exchange-186", f"review-exchange-{ISSUE}")
    v2 = _commit(rig.git, rig.worktree, "stale-alarm", "measured from the slot instant")
    assert _validate(rig, exchange, "review-exchange-186") == v2
    _push(rig)
    holds = _complete(rig, make_session, rework)
    rig.lifecycle.preserve_cleanup(ISSUE, f"review-exchange-{ISSUE}", rig.worktree, "session-cleanup")
    return v1, v2, holds


# -- capture ------------------------------------------------------------------


def _fact(rig):
    return rig.store.lineage_publication(canonical_lineage_key(ValidatedWorkKey(REPO, ISSUE, BRANCH, "0" * 40)))


def _published_as_contained(rig, heads, pr_head):
    by_head = {d.key.validated_head_sha: d for d in rig.store.for_issue(ISSUE).dispositions}
    for head in heads:
        record = rig.store.record_for_id(by_head[head].record_id)
        assert record.disposition.state is ValidatedWorkState.RECOVERED, record.disposition
        assert record.resolution_kind is ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD
        assert record.disposition.published_head_sha == pr_head
        # The PR it rests in is named, so published-review custody (#7293)
        # still refuses a reset that would close it.
        assert record.disposition.pr_number == PR
        # No validated commit is released: the escrow pin survives resolution.
        pinned = record.current_evidence.admission.pinned_ref
        assert rig.git.run(rig.repo, ["rev-parse", pinned]).stdout.strip() == head


def test_a_rework_its_completion_published_is_recorded_published_not_parked(rig, make_session):
    """The porchpin #186 regression, through `handle_session_completion` and the cleanup capture."""
    w1 = _recovery_published_first_head(rig)
    v1, v2, holds = _rework_published_by_its_completion(rig, make_session)
    assert rig.git.run(rig.repo, ["merge-base", "--is-ancestor", w1, v2], check=False).returncode != 0

    # Recovery routed nothing: a halted exchange must not defer to it (#7295).
    assert holds is False
    batch = rig.store.for_issue(ISSUE)
    assert not batch.unresolved, [(d.state, d.failure) for d in batch.dispositions]
    _published_as_contained(rig, (v1, v2), v2)
    fact = _fact(rig)
    assert (fact.published_head_sha, fact.published_via) == (v2, PublicationProvenance.OBSERVED_OPEN_PR)
    assert RECOVERY_PENDING not in rig.labels.labels
    assert rig.labels.operations == []


def test_a_later_rework_whose_push_failed_is_sequenced_from_the_prs_head(rig, make_session):
    """The lineage fact follows the PR, so the next cycle's unpublished head is
    recovery's ordinary descendant - not divergent from the stale ``W1``."""
    _recovery_published_first_head(rig)
    _, v2, _ = _rework_published_by_its_completion(rig, make_session)
    rig.git.run(rig.worktree, ["commit", "--allow-empty", "-q", "-m", "#186: rework cycle 2"])
    cycle2 = _run(rig, SessionKind.REWORK, "coding-3", f"rework-{ISSUE}")
    v3 = _validate(rig, cycle2, "coding-3")

    assert _complete(rig, make_session, cycle2) is True
    (held,) = [d for d in rig.store.for_issue(ISSUE).dispositions if d.key.validated_head_sha == v3]
    assert (held.state, held.lineage_role) == (ValidatedWorkState.QUEUED, LineageRole.HEAD)
    assert rig.store.record_for_id(held.record_id).current_evidence.admission.evidence \
        .observations.expected_remote_head_sha == v2
    assert RECOVERY_PENDING in rig.labels.labels


def test_a_rework_whose_push_failed_is_still_held_by_recovery(rig, make_session):
    """Fail-safe: the remote does not carry the rework's head, so recovery keeps it."""
    _recovery_published_first_head(rig)
    rig.git.run(rig.worktree, ["commit", "--allow-empty", "-q", "-m", "#186: address review"])
    rework = _run(rig, SessionKind.REWORK, "coding-2", f"rework-{ISSUE}")
    v1 = _validate(rig, rework, "coding-2")

    assert _complete(rig, make_session, rework) is True
    (held,) = [d for d in rig.store.for_issue(ISSUE).dispositions if d.key.validated_head_sha == v1]
    assert held.state is ValidatedWorkState.QUEUED  # a descendant of W1 from W1's baseline
    assert RECOVERY_PENDING in rig.labels.labels


@pytest.mark.parametrize("remote", ["pr-closed", "unreadable", "moved-past-another-head"])
def test_a_head_no_open_pr_is_proven_to_carry_is_left_to_recovery(rig, remote):
    """No proof, no publication: every doubt leaves the validated head held."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    _push(rig)
    if remote == "pr-closed":
        rig.github.open = False
    elif remote == "unreadable":
        rig.github.unreadable = True
    else:
        # The PR branch was force-pushed to a head that does not contain it.
        rig.git.run(rig.worktree, ["reset", "-q", "--hard", "main"])
        _commit(rig.git, rig.worktree, "other", "someone else's head")
        _push(rig)

    assert rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coding)
    assert rig.store.for_issue(ISSUE).unresolved
    assert _fact(rig) is None
    assert RECOVERY_PENDING in rig.labels.labels


def test_a_coding_run_that_published_its_own_pr_is_not_queued_for_recovery(rig):
    """#7340: the ordinary ending - the completion pushed and opened its PR -
    must not put ``recovery-pending`` on the issue while that PR goes to review."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    _push(rig)

    assert rig.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=coding) is False

    assert not rig.store.for_issue(ISSUE).unresolved
    _published_as_contained(rig, (w1,), w1)
    assert rig.labels.operations == []


# -- the store's lineage owner -----------------------------------------------


def _key(head):
    return ValidatedWorkKey(REPO, ISSUE, BRANCH, head)


def test_the_store_verifies_containment_itself(rig, make_session):
    w1 = _recovery_published_first_head(rig)
    ahead = _commit(rig.git, rig.worktree, "ahead", "not published")

    # A caller's proof that does not hold is refused, not trusted.
    assert rig.store.record_pr_publication(
        _key(ahead), published=PublishedOnOpenPullRequest(PR, w1), observed_at="2026-09-28T12:00:00+00:00",
    ) is PrPublicationStatus.CONTAINMENT_UNPROVEN
    assert _fact(rig).published_head_sha == w1


def test_the_fact_follows_a_pr_forced_back_to_an_ancestor(rig, make_session):
    """Review r3: the recorded head is W2, but the completion force-pushed its
    PR back to W1. The PR publishes W1 now; the completion published it."""
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    _validate(rig, coding, "coding-1")
    w2 = _commit(rig.git, rig.worktree, "more", "a later head recovery published")
    later = _run(rig, SessionKind.CODE, "coding-2", f"rework-{ISSUE}")
    _validate(rig, later, "coding-2")
    assert rig.lifecycle.preserve_completed_run(ISSUE, f"rework-{ISSUE}", "session-completion", run=later)
    (held,) = rig.store.for_issue(ISSUE).dispositions
    assert not isinstance(rig.store.resolve_observed_merge(
        record_id=held.record_id, merged_head_sha=w2, observed_at="2026-09-28T11:00:00+00:00"), str)
    rig.git.run(rig.worktree, ["reset", "-q", "--hard", w1])
    _push(rig)

    assert rig.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=coding) is False
    fact = _fact(rig)
    assert (fact.published_head_sha, fact.published_pr_number) == (w1, PR)
    (published,) = [d for d in rig.store.for_issue(ISSUE).dispositions if d.key.validated_head_sha == w1]
    assert published.published_by_its_pr
    # The same PR's publication of the same head is already recorded.
    assert rig.store.record_pr_publication(
        _key(w1), published=PublishedOnOpenPullRequest(PR, w1), observed_at="2026-09-28T12:00:00+00:00",
    ) is PrPublicationStatus.ALREADY_PUBLISHED


def test_a_record_another_owner_resolved_is_not_reported_published(rig, make_session, monkeypatch):
    """Review r3: the route is the durable record's. Another owner resolves the
    record between the sweep's proof and its result; the store refuses this
    publication, and the sweep must not claim it."""
    _, v2, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    (target,) = [d for d in parked if d.key.validated_head_sha == v2]

    def another_owner_first(key, *, published, observed_at):
        if key.validated_head_sha == v2:
            claim = rig.store.acquire_claim(target.record_id, expected_states=frozenset({ValidatedWorkState.PARKED}),
                                            evidence_id=target.evidence_id)
            assert rig.store.retire_outside_scope(claim, evidence_ids=frozenset({target.evidence_id}),
                                                  actor="another-owner", reason="resolved elsewhere")
            assert rig.store.relinquish_claim(claim)
        return PrPublicationStatus.PUBLICATION_IN_FLIGHT

    monkeypatch.setattr(rig.aggregate, "record_pr_publication", another_owner_first)

    report = _sweep(rig).tick(lambda: RecoveryDrainMode.ACTIVE)

    assert rig.store.get(target.record_id).state is ValidatedWorkState.ABANDONED
    assert report.published == ()


def test_an_in_flight_recovery_publication_keeps_the_lineage(rig, make_session):
    """§4.4e: a PUBLISHING record owns the remote expectation; nothing moves it."""
    w1 = _recovery_published_first_head(rig)
    held = _publishing_rework(rig, make_session)

    assert rig.store.record_pr_publication(
        _key(held.key.validated_head_sha), published=PublishedOnOpenPullRequest(PR, held.key.validated_head_sha),
        observed_at="2026-09-28T12:00:00+00:00",
    ) is PrPublicationStatus.PUBLICATION_IN_FLIGHT
    assert _fact(rig).published_head_sha == w1


# -- upgrade: records the regression already parked --------------------------


def _legacy_parked_records(rig, make_session, monkeypatch):
    """Capture exactly as the regressed engine did: no publication proof."""
    w1 = _recovery_published_first_head(rig)
    with monkeypatch.context() as legacy:
        legacy.setattr(PullRequestCarriage, "proof", lambda *_args, **_kwargs: None)
        v1, v2, holds = _rework_published_by_its_completion(rig, make_session)
    assert holds is True
    parked = [d for d in rig.store.for_issue(ISSUE).dispositions if d.state is ValidatedWorkState.PARKED]
    assert {d.key.validated_head_sha for d in parked} == {v1, v2}
    assert {d.failure for d in parked} == {ValidatedWorkFailure.DIVERGENT_VALIDATED_HEADS}
    assert RECOVERY_PENDING in rig.labels.labels
    return w1, v2, parked


def _sweep(rig):
    return OutOfScopeRetirementSweep(
        source=rig.store, store=rig.store, execution=rig.execution, retirement=rig.retirement,
        publication=PullRequestPublicationRecorder(
            carriage=PullRequestCarriage(git=rig.wc, observer=rig.github), store=rig.aggregate,
            repository=rig.repo, now=lambda: "2026-09-28T13:00:00+00:00"),
        batch_size=5, liveness=drain_liveness(records=rig.store))


def _drain(rig, sweep):
    return RecoveryDrain(
        queue=rig.store, operation=Mock(), authority_refresh=Mock(),
        claim_maintenance=NullRetainedClaimMaintenance(), block_sweep=NullRecoveryBlockSweep(),
        scope_sweep=sweep, batch_size=5, interval_seconds=1, liveness=drain_liveness(records=rig.store),
        clock=lambda instants=itertools.count(0, 10): next(instants))  # every tick is due


def test_upgrade_resolves_parked_records_its_completion_published(rig, make_session, monkeypatch):
    """The 7 porchpin records: the drain's scope sweep records the PR's head and
    the lineage resolves them, releasing the issue."""
    _, v2, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    drain = _drain(rig, _sweep(rig))

    report = drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    # The first record judged advanced the fact; its peers resolved with it.
    assert report.scope_sweep.retired == ()
    assert report.scope_sweep.published and set(report.scope_sweep.published) <= {d.record_id for d in parked}
    assert not rig.store.has_unresolved_work(ISSUE)
    _published_as_contained(rig, {d.key.validated_head_sha for d in parked}, v2)
    assert _fact(rig).published_via is PublicationProvenance.OBSERVED_OPEN_PR
    assert RECOVERY_PENDING not in rig.labels.labels
    for disposition in parked:
        assert rig.store.owner_of(disposition.record_id) is None


def test_a_parked_record_the_open_pr_does_not_carry_stays_parked_and_is_read_once(rig, make_session, monkeypatch):
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    rig.git.run(rig.worktree, ["reset", "-q", "--hard", "main"])
    _commit(rig.git, rig.worktree, "other", "a head that carries neither")
    _push(rig)
    drain = _drain(rig, _sweep(rig))

    drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)
    reads = rig.github.reads
    drain.tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    for disposition in parked:
        record = rig.store.record_for_id(disposition.record_id)
        assert record.disposition.state is ValidatedWorkState.PARKED
        assert record.disposition.lineage_role is LineageRole.DIVERGENT
        assert rig.store.owner_of(disposition.record_id) is None
    assert RECOVERY_PENDING in rig.labels.labels
    # Judged once per evidence and process: GitHub is not re-read every tick.
    assert rig.github.reads == reads


def _publishing_rework(rig, make_session):
    """A rework recovery claimed and began publishing; its push has landed."""
    rig.git.run(rig.worktree, ["commit", "--allow-empty", "-q", "-m", "#186: address review"])
    rework = _run(rig, SessionKind.REWORK, "coding-2", f"rework-{ISSUE}")
    v1 = _validate(rig, rework, "coding-2")
    assert _complete(rig, make_session, rework) is True
    (held,) = [d for d in rig.store.for_issue(ISSUE).dispositions if d.key.validated_head_sha == v1]
    claim = rig.store.acquire_claim(held.record_id, expected_states=frozenset({ValidatedWorkState.QUEUED}),
                                    evidence_id=held.evidence_id)
    assert rig.store.begin_publish_attempt(claim, expected_attempt_no=0, target_head_sha=v1,
        expected_remote_head=rig.git.run(rig.origin, ["rev-parse", BRANCH]).stdout.strip(),
        phase=DispositionPhase.PRE_SUBMISSION, started_at="2026-09-28T11:50:00+00:00") is not None
    _push(rig)  # recovery's own push landed; its finalization has not run yet
    assert rig.store.relinquish_claim(claim)
    return held


def test_recoverys_own_publication_is_left_to_its_finalizer(rig, make_session):
    """A record recovery is publishing finishes through its finalizer, which
    routes the PR to review; the sweep does not even read the remote for it."""
    _recovery_published_first_head(rig)
    held = _publishing_rework(rig, make_session)
    reads = rig.github.reads

    report = _sweep(rig).tick(lambda: RecoveryDrainMode.ACTIVE)

    assert (report.retired, report.published) == ((), ())
    assert rig.store.get(held.record_id).state is ValidatedWorkState.PUBLISHING
    assert rig.github.reads == reads


# -- the rule ------------------------------------------------------------------


def _facts(head, *, prs=1, state=PublicationPrState.OPEN, branch=BRANCH, repo=REPO, pr_head=None):
    return ValidatedWorkRemoteFacts(head, tuple(PublicationPullRequest(
        PR + n, f"https://github.com/{REPO}/pull/{PR + n}", repo, repo, branch, "main", pr_head or head,
        state, "",
    ) for n in range(prs)))


HEAD, OTHER = "a" * 40, "b" * 40


@pytest.mark.parametrize(("facts", "fetched", "relation"), [
    (_facts(HEAD, prs=0), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD, prs=2), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD, state=PublicationPrState.CLOSED), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD, branch="another-branch"), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD, repo="fork/repo"), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD, pr_head=OTHER), HEAD, AncestryRelation.EQUAL),
    (_facts(HEAD), OTHER, AncestryRelation.EQUAL),
    (_facts(HEAD), None, None),
    (_facts(HEAD), HEAD, AncestryRelation.DESCENDANT),
    (_facts(HEAD), HEAD, AncestryRelation.DIVERGENT),
    (_facts(HEAD), HEAD, AncestryRelation.LEFT_UNREACHABLE),
], ids=["no-pr", "two-prs", "closed", "other-branch", "fork", "pr-head-stale", "moved-since-read",
        "unfetchable", "head-ahead-of-pr", "divergent", "unreachable"])
def test_only_every_fact_agreeing_proves_publication(facts, fetched, relation):
    assert carried_by_open_pull_request(
        facts, repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=fetched, relation=relation) is None


@pytest.mark.parametrize("relation", [AncestryRelation.EQUAL, AncestryRelation.ANCESTOR])
def test_an_open_pr_carrying_the_head_proves_publication(relation):
    proof = carried_by_open_pull_request(
        _facts(HEAD), repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=HEAD, relation=relation)
    assert proof == PublishedOnOpenPullRequest(PR, HEAD)
    assert f"open PR #{PR}" in proof.describe(OTHER)


def test_the_admission_only_store_records_an_open_prs_publication_the_same_way(tmp_path):
    """The admission-only composition shares the one lineage owner."""
    from issue_orchestrator.infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore
    from tests.unit.validated_work_support import L, V, ArtifactVerifier, GraphAncestry, capture

    store = SqliteValidatedWorkIntakeStore(tmp_path / "work.sqlite", GraphAncestry(), ArtifactVerifier())
    admission = capture(V, state=ValidatedWorkState.PARKED,
                        failure=ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION, reason="ahead")
    store.admit(admission)
    key = admission.evidence.identity.key

    status = store.record_pr_publication(
        key, published=PublishedOnOpenPullRequest(91, L), observed_at="2026-09-28T13:00:00+00:00")

    assert status is PrPublicationStatus.ADVANCED
    (disposition,) = store.for_issue(key.issue_number).dispositions
    assert (disposition.state, disposition.published_head_sha) == (ValidatedWorkState.RECOVERED, L)


# -- review round 1 -------------------------------------------------------------


def test_published_work_whose_escrow_fails_to_verify_is_still_held_by_recovery(rig, monkeypatch):
    """Only a capture admission actually resolved inside the PR's head is the
    completion's: evidence that fails verification stays FAILED, recovery's."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    _push(rig)
    # Escrow is intact at capture, but containment's re-verification fails.
    from issue_orchestrator.infra.validated_work_lineage import LineageClassifier
    monkeypatch.setattr(LineageClassifier, "verifies", lambda _self, _evidence: False)

    assert rig.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=coding) is True
    (failed,) = rig.store.for_issue(ISSUE).dispositions
    assert (failed.state, failed.failure) == (ValidatedWorkState.FAILED, ValidatedWorkFailure.ARTIFACT_HASH_MISMATCH)
    assert _fact(rig).published_via is PublicationProvenance.OBSERVED_OPEN_PR


def test_a_record_captured_before_its_pr_existed_rests_in_the_pr_that_publishes_it(rig):
    """Published-review custody matches a PR by number or exact head: a record
    resolved by a later open PR must name that PR, or the next push to it
    leaves the PR unguarded against a reset."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    assert rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coding)
    (held,) = rig.store.for_issue(ISSUE).dispositions
    assert held.pr_number is None  # no PR when it was captured
    _commit(rig.git, rig.worktree, "more", "pushed later by another completion")
    _push(rig)

    report = _sweep(rig).tick(lambda: RecoveryDrainMode.ACTIVE)

    assert report.published == (held.record_id,)
    resolved = rig.store.get(held.record_id)
    assert (resolved.state, resolved.pr_number) == (ValidatedWorkState.RECOVERED, PR)
    assert resolved.key.validated_head_sha == w1


def test_a_publication_whose_block_projection_fails_is_not_reported_retired(rig, make_session, monkeypatch):
    _, v2, parked = _legacy_parked_records(rig, make_session, monkeypatch)

    def unreadable(_issue):
        raise RuntimeError("GitHub label read failed")

    monkeypatch.setattr(rig.labels, "read_issue_labels", unreadable)

    report = _sweep(rig).tick(lambda: RecoveryDrainMode.ACTIVE)

    # Reported by the durable resolution, never as a retirement.
    assert report.retired == ()
    assert report.published and set(report.published) <= {d.record_id for d in parked}
    # The publication itself committed; the block sweep reconciles the labels.
    assert not rig.store.has_unresolved_work(ISSUE)
    assert _fact(rig).published_head_sha == v2


def test_an_existing_store_gains_the_published_pr_column_on_open(tmp_path):
    import sqlite3

    from issue_orchestrator.infra.validated_work_rows import DispositionDatabase

    path = tmp_path / "work.sqlite"
    DispositionDatabase(path)
    tables = ("validated_work_records", "validated_work_lineage")
    with sqlite3.connect(path) as conn:  # a store from before this column existed
        for table in tables:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN published_pr_number")

    DispositionDatabase(path)

    with sqlite3.connect(path) as conn:
        for table in tables:
            assert "published_pr_number" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_cold_reader_refuses_an_unmigrated_store_instead_of_crashing(tmp_path):
    """Review r2: the mapper reads ``published_pr_number``, so a read-only reader
    of a store the engine has not yet migrated reports an unsupported schema."""
    import sqlite3

    from issue_orchestrator.domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure
    from issue_orchestrator.infra.validated_work_read_schema import require_supported_validated_work_schema
    from issue_orchestrator.infra.validated_work_rows import DispositionDatabase

    path = tmp_path / "work.sqlite"
    DispositionDatabase(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE validated_work_records DROP COLUMN published_pr_number")
        conn.row_factory = sqlite3.Row
        with pytest.raises(ReadOnlySqliteAccessError) as refused:
            require_supported_validated_work_schema(conn)
    assert refused.value.reason is ReadOnlySqliteFailure.UNSUPPORTED_SCHEMA
    assert "published_pr_number" in str(refused.value)


def test_a_replayed_capture_still_says_recovery_holds_nothing(rig):
    """Review r2: the answer is the durable record's, so re-running the
    terminal capture (a crash-replayed completion) gives it again."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    _push(rig)

    first = rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coding)
    replayed = rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=coding)

    assert (first, replayed) == (False, False)
    assert len(rig.store.retained_evidence(ISSUE)) == 1
    assert rig.labels.operations == []


def test_a_parked_record_published_later_is_released_on_the_next_recheck(rig, make_session, monkeypatch):
    """Review r2: a later push can publish a parked head with no new capture;
    the sweep asks again after its recheck interval, and not before."""
    _, v2, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    published_head = rig.git.head_sha(rig.worktree)
    rig.git.run(rig.worktree, ["reset", "-q", "--hard", "main"])
    _commit(rig.git, rig.worktree, "other", "a head that carries neither")
    _push(rig)
    now = [1000.0]
    sweep = OutOfScopeRetirementSweep(
        source=rig.store, store=rig.store, execution=rig.execution, retirement=rig.retirement,
        publication=PullRequestPublicationRecorder(
            carriage=PullRequestCarriage(git=rig.wc, observer=rig.github), store=rig.aggregate,
            repository=rig.repo, now=lambda: "2026-09-28T13:00:00+00:00"),
        batch_size=5, liveness=drain_liveness(records=rig.store),
        publication_recheck_seconds=600, clock=lambda: now[0])

    sweep.tick(lambda: RecoveryDrainMode.ACTIVE)
    assert rig.store.has_unresolved_work(ISSUE)
    rig.git.run(rig.worktree, ["reset", "-q", "--hard", published_head])
    _push(rig)  # the PR now carries the parked heads again
    reads = rig.github.reads
    sweep.tick(lambda: RecoveryDrainMode.ACTIVE)  # inside the interval: no read
    assert rig.github.reads == reads
    now[0] += 600

    report = sweep.tick(lambda: RecoveryDrainMode.ACTIVE)

    assert report.published
    assert not rig.store.has_unresolved_work(ISSUE)
    _published_as_contained(rig, {d.key.validated_head_sha for d in parked}, v2)
    assert RECOVERY_PENDING not in rig.labels.labels


@pytest.mark.parametrize("superseded_by", ["rebase", "force-back-to-ancestor"])
def test_the_completion_superseding_its_own_publication_reopens_nothing(rig, make_session, superseded_by):
    """Review r4 (declined, by design): work the completion published is not a
    recovery obligation, so when that same owner's later push replaces it - a
    rework rebase, or a force-push back - recovery does not reopen it. Reopening
    it would park it and re-block the PR under review: the #186 regression."""
    first = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, first, "coding-1")
    w2 = _commit(rig.git, rig.worktree, "more", "second commit")
    second = _run(rig, SessionKind.REWORK, "coding-2", f"rework-{ISSUE}")
    _validate(rig, second, "coding-2")
    _push(rig)
    assert rig.lifecycle.preserve_completed_run(ISSUE, f"rework-{ISSUE}", "session-completion", run=second) is False
    (w2_record,) = [d for d in rig.store.for_issue(ISSUE).dispositions if d.key.validated_head_sha == w2]
    assert w2_record.published_by_its_pr
    if superseded_by == "rebase":
        _rework_rebases_onto_moved_main(rig)
    else:
        rig.git.run(rig.worktree, ["reset", "-q", "--hard", w1])
    _push(rig)

    assert rig.lifecycle.preserve_completed_run(ISSUE, f"issue-{ISSUE}", "session-completion", run=first) is False

    assert rig.store.get(w2_record.record_id).state is ValidatedWorkState.RECOVERED
    assert not rig.store.has_unresolved_work(ISSUE)
    assert RECOVERY_PENDING not in rig.labels.labels


# -- merged PRs (porchpin #26, #320) ------------------------------------------
#
# After #7497 resolved records an OPEN PR carries, three porchpin records stayed
# ``divergent_validated_heads``: their PRs had been squash-merged. There is no
# open PR head any more, the branch may be deleted, and the squash commit on
# ``main`` contains none of the original commits. The proof is the merged PR's
# head at merge - GitHub keeps it at ``refs/pull/N/head`` - and the record's
# validated head is that head or one of its ancestors. The bare remote plays
# GitHub's squash merge: a new commit on ``main``, the head branch deleted,
# ``refs/pull/381/head`` left at the PR's last head.


def _squash_merge(rig) -> str:
    """GitHub's squash merge of PR #381, then its head branch deleted."""
    pr_head = rig.git.run(rig.origin, ["rev-parse", f"refs/heads/{BRANCH}"]).stdout.strip()
    rig.git.run(rig.origin, ["update-ref", f"refs/pull/{PR}/head", pr_head])
    rig.git.run(rig.repo, ["fetch", "-q", "origin", "main", BRANCH])
    rig.git.run(rig.repo, ["checkout", "-q", "--detach", "origin/main"])
    rig.git.run(rig.repo, ["merge", "-q", "--squash", f"origin/{BRANCH}"])
    rig.git.run(rig.repo, ["commit", "-q", "-m", f"#186 (#{PR})"])
    rig.git.run(rig.repo, ["push", "-q", "origin", "HEAD:refs/heads/main", f":refs/heads/{BRANCH}"])
    rig.github.merged = True
    return pr_head


def test_parked_records_a_squash_merged_pr_landed_resolve(rig, make_session, monkeypatch):
    w1, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    pr_head = _squash_merge(rig)
    squash = rig.git.run(rig.origin, ["rev-parse", "main"]).stdout.strip()
    for disposition in parked:  # the squash commit does not contain the work
        assert rig.git.run(rig.repo, ["merge-base", "--is-ancestor", disposition.key.validated_head_sha, squash],
                           check=False).returncode != 0

    report = _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    assert report.scope_sweep.retired == ()
    assert report.scope_sweep.published
    assert not rig.store.has_unresolved_work(ISSUE)
    for disposition in parked:
        record = rig.store.record_for_id(disposition.record_id)
        assert record.disposition.state is ValidatedWorkState.RECOVERED
        assert record.resolution_kind is ResolutionKind.LANDED_VIA_MERGED_PR
        assert record.disposition.published_head_sha == pr_head
        assert record.disposition.pr_number == PR
        pinned = record.current_evidence.admission.pinned_ref
        assert rig.git.run(rig.repo, ["rev-parse", pinned]).stdout.strip() == disposition.key.validated_head_sha
    # A landing sits beside the lineage fact; it never replaces it (§2.8).
    assert _fact(rig).published_head_sha == w1
    assert RECOVERY_PENDING not in rig.labels.labels


def test_a_capture_after_its_pr_merged_records_the_landing(rig, make_session):
    """The same owner at capture: a run whose PR was squash-merged before its
    terminal capture (a late session-cleanup) is landed, not parked."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    w1 = _commit(rig.git, rig.worktree, "journey", "first attempt")
    _validate(rig, coding, "coding-1")
    _push(rig)
    pr_head = _squash_merge(rig)

    assert rig.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=coding) is False

    (landed,) = rig.store.for_issue(ISSUE).dispositions
    record = rig.store.record_for_id(landed.record_id)
    assert (record.disposition.state, record.resolution_kind) == (
        ValidatedWorkState.RECOVERED, ResolutionKind.LANDED_VIA_MERGED_PR)
    assert (pr_head, record.disposition.pr_number) == (w1, PR)
    assert rig.labels.operations == []


@pytest.mark.parametrize("doubt", ["not-carried", "pull-ref-gone", "unreadable"])
def test_a_merged_pr_that_is_not_proven_to_have_landed_it_leaves_it_parked(rig, make_session, monkeypatch, doubt):
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    _squash_merge(rig)
    if doubt == "not-carried":
        # GitHub reports a head at merge that carries none of the parked work.
        rig.git.run(rig.repo, ["checkout", "-q", "--detach", "origin/main~1"])
        rig.git.run(rig.repo, ["commit", "-q", "--allow-empty", "-m", "unrelated"])
        rig.git.run(rig.repo, ["push", "-q", "origin", f"HEAD:refs/pull/{PR}/head", "--force"])
    elif doubt == "pull-ref-gone":
        rig.git.run(rig.origin, ["update-ref", "-d", f"refs/pull/{PR}/head",
                                 rig.git.run(rig.origin, ["rev-parse", f"refs/pull/{PR}/head"]).stdout.strip()])
        monkeypatch.setattr(rig.github, "merged_pull_requests", lambda request: (PublicationPullRequest(
            PR, f"https://github.com/{REPO}/pull/{PR}", REPO, REPO, BRANCH, "main", parked[0].key.validated_head_sha,
            PublicationPrState.MERGED, ""),))
    else:
        rig.github.unreadable = True

    _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    for disposition in parked:
        assert rig.store.get(disposition.record_id).state is ValidatedWorkState.PARKED
    assert RECOVERY_PENDING in rig.labels.labels


def _merged(head, *, state=PublicationPrState.MERGED, branch=BRANCH, repo=REPO):
    return PublicationPullRequest(PR, f"https://github.com/{REPO}/pull/{PR}", repo, repo, branch, "main",
                                  head, state, "")


@pytest.mark.parametrize(("pr", "fetched", "relation"), [
    (_merged(HEAD, state=PublicationPrState.CLOSED), HEAD, AncestryRelation.EQUAL),
    (_merged(HEAD, state=PublicationPrState.OPEN), HEAD, AncestryRelation.EQUAL),
    (_merged(HEAD, branch="another-branch"), HEAD, AncestryRelation.EQUAL),
    (_merged(HEAD, repo="fork/repo"), HEAD, AncestryRelation.EQUAL),
    (_merged(HEAD), OTHER, AncestryRelation.EQUAL),
    (_merged(HEAD), None, None),
    (_merged(HEAD), HEAD, AncestryRelation.DESCENDANT),
    (_merged(HEAD), HEAD, AncestryRelation.DIVERGENT),
    (_merged(HEAD), HEAD, AncestryRelation.RIGHT_UNREACHABLE),
], ids=["closed-unmerged", "open", "other-branch", "fork", "pull-ref-moved", "pull-ref-unfetchable",
        "head-ahead-of-pr", "divergent", "unreachable"])
def test_only_every_fact_agreeing_proves_a_landing(pr, fetched, relation):
    assert landed_via_merged_pull_request(
        pr, repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=fetched, relation=relation) is None


@pytest.mark.parametrize("relation", [AncestryRelation.EQUAL, AncestryRelation.ANCESTOR])
def test_a_merged_pr_whose_head_carries_the_work_proves_the_landing(relation):
    proof = landed_via_merged_pull_request(
        _merged(HEAD), repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=HEAD, relation=relation)
    assert proof == LandedViaMergedPullRequest(PR, HEAD)
    assert proof.provenance is PublicationProvenance.OBSERVED_MERGE



# -- landings beside the fact (review r1) ---------------------------------------


def _graph_store(tmp_path):
    from tests.unit.validated_work_support import Rig
    return Rig(tmp_path / "work.sqlite")


def test_a_landing_never_strands_work_sequenced_from_a_newer_open_pr(tmp_path):
    """Review r1: on a reused branch, an older merged PR landed ``DIVERGENT``
    while a newer open PR publishes ``TIP``. Proving the old landing resolves
    the record it carries, and leaves the open PR's fact - and the unpublished
    descendant sequenced from it - exactly as they were."""
    from tests.unit.validated_work_support import DIVERGENT, ROOT, TIP, capture

    rig = _graph_store(tmp_path)
    beyond = f"{9:040x}"
    rig.graph.parents[beyond] = TIP
    store = rig.open()
    old = capture(DIVERGENT, run="run-old", state=ValidatedWorkState.PARKED,
                  failure=ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION, reason="captured", expected=ROOT)
    store.admit(old)
    open_key = old.evidence.identity.key.__class__("owner/repo", 6914, "feature", TIP)
    assert store.record_pr_publication(
        open_key, published=PublishedOnOpenPullRequest(92, TIP), observed_at="2026-09-30T10:00:00+00:00",
    ) is PrPublicationStatus.ADVANCED
    newer = capture(beyond, run="run-new", expected=TIP, pr=92)
    store.admit(newer)
    # The two unresolved heads diverge, so both park until one resolves.
    assert {store.get(r.evidence.record_id).failure for r in (old, newer)} == {
        ValidatedWorkFailure.DIVERGENT_VALIDATED_HEADS}

    status = store.record_pr_publication(
        old.evidence.identity.key, published=LandedViaMergedPullRequest(91, DIVERGENT),
        observed_at="2026-09-30T11:00:00+00:00",
    )

    assert status is PrPublicationStatus.ADVANCED
    landed = store.get(old.evidence.record_id)
    assert (landed.state, landed.pr_number, landed.published_head_sha) == (ValidatedWorkState.RECOVERED, 91, DIVERGENT)
    assert store.record_for_id(old.evidence.record_id).resolution_kind is ResolutionKind.LANDED_VIA_MERGED_PR
    fact = store.lineage_publication(canonical_lineage_key(open_key))
    assert (fact.published_head_sha, fact.published_pr_number) == (TIP, 92)
    still = store.get(newer.evidence.record_id)
    assert (still.state, still.lineage_role) == (ValidatedWorkState.QUEUED, LineageRole.HEAD)


def test_an_open_prs_merge_at_the_same_head_lands_what_it_carried(tmp_path):
    """Review r1: PR 91 was recorded open at ``L``, then merged without a new
    push. Its landing is new, and a later capture it contains is LANDED."""
    from tests.unit.validated_work_support import L, ROOT, V, capture

    store = _graph_store(tmp_path).open()
    key = capture(L).evidence.identity.key
    assert store.record_pr_publication(
        key, published=PublishedOnOpenPullRequest(91, L), observed_at="2026-09-30T10:00:00+00:00",
    ) is PrPublicationStatus.ADVANCED

    assert store.record_pr_publication(
        key, published=LandedViaMergedPullRequest(91, L), observed_at="2026-09-30T11:00:00+00:00",
    ) is PrPublicationStatus.ADVANCED
    late = capture(V, run="run-late", expected=ROOT)
    store.admit(late)

    assert store.record_for_id(late.evidence.record_id).resolution_kind is ResolutionKind.LANDED_VIA_MERGED_PR
    # The same merged PR proven again is already recorded; a moved head is refused.
    assert store.record_pr_publication(
        key, published=LandedViaMergedPullRequest(91, L), observed_at="2026-09-30T12:00:00+00:00",
    ) is PrPublicationStatus.ALREADY_PUBLISHED
    assert store.record_pr_publication(
        capture(V).evidence.identity.key, published=LandedViaMergedPullRequest(91, V),
        observed_at="2026-09-30T12:00:00+00:00",
    ) is PrPublicationStatus.CONTAINMENT_UNPROVEN


def test_an_existing_store_gains_the_landings_table_on_open(tmp_path):
    import sqlite3

    from issue_orchestrator.infra.validated_work_rows import DispositionDatabase

    path = tmp_path / "work.sqlite"
    DispositionDatabase(path)
    with sqlite3.connect(path) as conn:  # a store from before landings existed
        conn.execute("DROP TABLE validated_work_lineage_landings")

    DispositionDatabase(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='validated_work_lineage_landings'"
        ).fetchone()
