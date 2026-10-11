"""The tech lead's ``release_withheld_review`` owner (#7399).

Every precondition is re-verified at apply time through the owner that answers
it; a failed one refuses the release with a typed code and writes nothing.
These tests drive the executor through its injected owner seams and read only
what crosses them: the writes it hands the applier, and the events it
publishes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.human_gates import HumanGates
from issue_orchestrator.control.action_results import ActionResult
from issue_orchestrator.control.actions import (
    Action,
    AddLabelAction,
    ReleaseWithheldReviewAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.pr_scanner import PRScanner
from issue_orchestrator.control.published_review_custody import PublishedReviewHold
from issue_orchestrator.control.published_review_release import ReviewReleaseWrites
from issue_orchestrator.control.review_exchange_lifecycle import (
    IssueRuntimeActivity,
    IssueRuntimeOwnerKind,
)
from issue_orchestrator.control.tech_lead_review_release import (
    ReviewReleaseRefusal,
    TechLeadReviewReleaseExecutor,
)
from issue_orchestrator.domain.models import (
    Issue,
    PendingTechLeadReview,
    PendingValidationRetry,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pending_work_claim_store import UnreadableClaim, UnresolvedClaim
from issue_orchestrator.control.issue_work_claims import claims_on_issue
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.pull_request_tracker import PRInfo, StatusCheckRollupRead
from issue_orchestrator.control.session_history import SessionHistoryOwner

ISSUE = 7395
PR = 7396
ANCHOR = 7395
REVIEW_LABEL = "needs-code-review"
OBSERVED = "2026-09-27T14:12:09+00:00"
BEFORE = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 9, 27, 14, 30, tzinfo=timezone.utc)


def _config(filter_label: str | None = None) -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = REVIEW_LABEL
    config.filtering.label = filter_label
    return config


def _issue(*labels: str, state: str = "open") -> Issue:
    return Issue(
        number=ISSUE,
        title="Blocked issue whose green PR waits on review",
        labels=list(labels or ("blocked-failed", "pr-pending", "agent:exam-coder")),
        state=state,
        repo="owner/repo",
    )


def _pr(number: int = PR, *, branch: str = f"{ISSUE}-green", labels: tuple[str, ...] = (REVIEW_LABEL,),
        body: str = f"Closes #{ISSUE}") -> PRInfo:
    return PRInfo(
        number=number, title=f"#{ISSUE}: work", url=f"https://github.com/owner/repo/pull/{number}",
        branch=branch, body=body, state="open", labels=list(labels), draft=True, head_sha="a" * 40,
    )


SOURCE_SESSION = "tech-lead-7395"


def _claim(
    kind: PendingWorkKind, *, session: str = "issue-7395", started_at: str = OBSERVED
) -> UnresolvedClaim:
    if kind is PendingWorkKind.TECH_LEAD:
        request: object = PendingTechLeadReview(
            ISSUE, "investigation", flavor=TechLeadSessionFlavor.BATCH_REVIEW
        )
    else:
        request = PendingValidationRetry(
            issue_number=ISSUE, issue_title="t", agent_label="agent:exam-coder",
            worktree_path="/tmp/w", branch_name=f"{ISSUE}-green", original_prompt="p",
            validation_error="boom", validation_error_file=None, retry_count=1,
            source_kind=SessionKind.CODE, validation_cmd="make test",
        )
    return UnresolvedClaim(
        run_key="run", session_name=session, deferred=True, started_at=started_at,
        issue_number=ISSUE, claim=PendingWorkClaim(kind=kind, request=request),  # type: ignore[arg-type]
    )


def _history(status: str, completed_at: datetime | None) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=ISSUE, title="t", agent_type="agent:exam-coder",
        status=status, runtime_minutes=3, completed_at=completed_at,  # type: ignore[arg-type]
    )


@dataclass
class Board:
    """Every owner's answer, as a test sets it; and every write it received."""

    issue: Issue | None = field(default_factory=_issue)
    prs: list[PRInfo] = field(default_factory=lambda: [_pr()])
    #: What a fresh single-PR read returns, when it differs from ``prs``.
    live: dict[int, PRInfo | None] = field(default_factory=dict)
    branches: dict[int, str] = field(default_factory=dict)
    checks: StatusCheckRollupRead = field(default_factory=lambda: StatusCheckRollupRead("SUCCESS"))
    activity: IssueRuntimeActivity = field(
        default_factory=lambda: IssueRuntimeActivity(frozenset(), frozenset())
    )
    claims: list[UnresolvedClaim] = field(default_factory=list)
    unreadable: list[UnreadableClaim] = field(default_factory=list)
    discoverable: bool = True
    filter_label: str | None = None
    history: list[SessionHistoryEntry] = field(default_factory=list)
    holds: tuple[PublishedReviewHold, ...] = ()
    fail_writes: frozenset[type] = frozenset()
    #: Run when the PR's review label is written (another owner acting meanwhile).
    during_route: "Callable[[Board], None] | None" = None
    writes: list[Action] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)

    def apply(self, action: Action) -> ActionResult:
        self.writes.append(action)
        routing = isinstance(action, AddLabelAction) and action.issue_number == PR
        if routing and self.during_route is not None:
            self.during_route(self)
        if type(action) in self.fail_writes:
            return ActionResult.fail(action, "write refused")
        return ActionResult.ok(action)

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)

    def read_pr(self, number: int) -> PRInfo | None:
        if number in self.live:
            return self.live[number]
        return next((pr for pr in self.prs if pr.number == number), None)

    # The claim store's two listings, read through the real per-issue owner.
    def list_unresolved_claims(self) -> tuple[UnresolvedClaim, ...]:
        return tuple(self.claims)

    def list_unreadable_claims(self) -> tuple[UnreadableClaim, ...]:
        return tuple(self.unreadable)

    def executor(self) -> TechLeadReviewReleaseExecutor:
        config = _config(self.filter_label)
        # Review discovery's REAL per-PR gate, over this board's issue.
        scanner = PRScanner(
            config=config, repository=SimpleNamespace(get_issue=lambda number: self.issue),  # type: ignore[arg-type]
            events=MagicMock(), issue_branches_fn=lambda: self.branches,
        )
        labels = LabelManager(config)
        history = SessionHistoryOwner(self.history)
        custody = self

        class _Custody:
            def holds(self, issue_number: int) -> tuple[PublishedReviewHold, ...]:
                return custody.holds

        return TechLeadReviewReleaseExecutor(
            events=self,  # type: ignore[arg-type]
            config=config,
            labels=labels,
            gates=HumanGates.unrecorded(labels),
            read_issue=lambda number: self.issue,
            # Like the real complete listing: no labels, no draft flag.
            list_open_prs=lambda: [replace(pr, labels=[], draft=None) for pr in self.prs],
            read_pr=self.read_pr,
            issue_branches=lambda: self.branches,
            review_admission=scanner.review_admission,
            read_checks=lambda number: self.checks,
            runtime_activity=lambda number: self.activity,
            claims_on_issue=lambda number: claims_on_issue(self, number),  # type: ignore[arg-type]
            failures_not_before=history.failures_not_before,
            custody=_Custody(),
            reviews_discoverable=lambda: self.discoverable,
            writes=ReviewReleaseWrites(labels=labels, apply=self.apply, review_label=REVIEW_LABEL),
            repo_slug="owner/repo",
        )


def _action(**overrides: object) -> ReleaseWithheldReviewAction:
    fields: dict[str, object] = {
        "issue_number": ISSUE, "rationale": "only blocked-failed withholds it",
        "proposal_id": "A2", "finding_ids": ("T1",), "anchor_issue_number": ANCHOR,
        "observed_at": OBSERVED, "source_session_name": SOURCE_SESSION,
    }
    fields.update(overrides)
    return ReleaseWithheldReviewAction(**fields)  # type: ignore[arg-type]


def _event_names(board: Board) -> list[str]:
    return [event.event_type.value for event in board.events]


def test_a_withheld_green_pr_is_released_in_the_shared_order() -> None:
    board = Board()

    result = board.executor().apply(_action())

    assert result.success, result.error
    assert result.details["pr_number"] == PR
    # pr-pending first (no coder may launch over the PR), the review route,
    # then ONLY blocked-failed, guarded on pr-pending and against needs-human.
    assert [(type(w).__name__, getattr(w, "label", None), getattr(w, "issue_number", None))
            for w in board.writes] == [
        ("AddLabelAction", "pr-pending", ISSUE),
        ("AddLabelAction", REVIEW_LABEL, PR),
        ("RemoveLabelAction", "blocked-failed", ISSUE),
    ]
    lift = board.writes[-1]
    assert isinstance(lift, RemoveLabelAction) and lift.expected is not None
    assert _event_names(board) == [EventName.TECH_LEAD_ACTION_EXECUTED.value]
    payload = board.events[0].data
    assert payload["proposal_type"] == "release_withheld_review"
    assert payload["action_id"] == "A2"
    assert payload["target_number"] == ISSUE
    assert payload["issue_number"] == ANCHOR


def _refused(board: Board, action: ReleaseWithheldReviewAction | None = None) -> ActionResult:
    result = board.executor().apply(action or _action())
    assert not result.success
    assert board.writes == [], "a refused release must write nothing"
    assert _event_names(board) == [EventName.TECH_LEAD_ACTION_PROPOSED.value]
    payload = board.events[0].data
    assert payload["mode"] == "stale_downgrade"
    assert payload["boundary"] == {"refusal": result.details["refusal"]}
    assert payload["stale_reason"].startswith(result.details["refusal"])
    return result


@pytest.mark.parametrize(
    ("board", "code"),
    [
        (Board(activity=IssueRuntimeActivity(frozenset({IssueRuntimeOwnerKind.SESSIONS}), frozenset())),
         ReviewReleaseRefusal.LIVE_SESSION),
        (Board(activity=IssueRuntimeActivity(frozenset(), frozenset({IssueRuntimeOwnerKind.EXCHANGE_JOBS}))),
         ReviewReleaseRefusal.LIVE_SESSION),
        (Board(discoverable=False), ReviewReleaseRefusal.REVIEW_DISCOVERY_DISABLED),
        (Board(claims=[_claim(PendingWorkKind.VALIDATION_RETRY)]), ReviewReleaseRefusal.WORK_CLAIMED),
        # Another tech lead's claim is not the proposer's (same session, other generation too).
        (Board(claims=[_claim(PendingWorkKind.TECH_LEAD, session="tech-lead-7000")]),
         ReviewReleaseRefusal.WORK_CLAIMED),
        (Board(claims=[_claim(PendingWorkKind.TECH_LEAD, session=SOURCE_SESSION, started_at=AFTER.isoformat())]),
         ReviewReleaseRefusal.WORK_CLAIMED),
        # A row nobody can read proves nothing is owed.
        (Board(unreadable=[UnreadableClaim("run", SOURCE_SESSION, ISSUE, "payload corrupt", OBSERVED)]),
         ReviewReleaseRefusal.WORK_CLAIMED),
        (Board(history=[_history("failed", AFTER)]), ReviewReleaseRefusal.NEWER_FAILURE),
        (Board(history=[_history("blocked", None)]), ReviewReleaseRefusal.NEWER_FAILURE),
        (Board(issue=None), ReviewReleaseRefusal.ISSUE_UNREADABLE),
        (Board(issue=_issue("blocked-failed", "pr-pending", state="closed")), ReviewReleaseRefusal.ISSUE_CLOSED),
        (Board(prs=[]), ReviewReleaseRefusal.NO_OPEN_PR),
        (Board(prs=[_pr(body="Closes #1", branch="1-other")]), ReviewReleaseRefusal.NO_OPEN_PR),
        (Board(branches={ISSUE: f"{ISSUE}-newer-attempt"}), ReviewReleaseRefusal.NO_OPEN_PR),
        (Board(prs=[_pr(), _pr(7397, branch=f"{ISSUE}-green")]), ReviewReleaseRefusal.SEVERAL_OPEN_PRS),
        # Closed (or gone) between the listing and the fresh read.
        (Board(live={PR: replace(_pr(), state="closed")}), ReviewReleaseRefusal.NO_OPEN_PR),
        (Board(live={PR: None}), ReviewReleaseRefusal.NO_OPEN_PR),
        # Review discovery's own gate judges the fresh PR: out of scope ...
        (Board(filter_label="io:e2e:run"), ReviewReleaseRefusal.REVIEW_NOT_DISCOVERABLE),
        # ... moved to another branch than the active one since the listing ...
        (Board(branches={ISSUE: f"{ISSUE}-green"}, live={PR: _pr(branch=f"{ISSUE}-renamed")}),
         ReviewReleaseRefusal.REVIEW_NOT_DISCOVERABLE),
        # ... or relinked to another issue.
        (Board(live={PR: _pr(branch="1-other", body="Closes #1")}),
         ReviewReleaseRefusal.REVIEW_NOT_DISCOVERABLE),
        # The fresh read is what validity judges: a block that landed on the PR.
        (Board(live={PR: _pr(labels=(REVIEW_LABEL, "blocked-failed"))}),
         ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK),
        (Board(holds=(PublishedReviewHold(ISSUE, 7000, f"{ISSUE}-held", "rec", "b" * 40),)),
         ReviewReleaseRefusal.PUBLISHED_WORK_ON_ANOTHER_PR),
        (Board(issue=_issue("pr-pending")), ReviewReleaseRefusal.REVIEW_NOT_WITHHELD),
        (Board(issue=_issue("blocked-failed", "pr-pending", "needs-human")),
         ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK),
        (Board(issue=_issue("blocked-failed", "pr-pending", "needs-rework")),
         ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK),
        (Board(prs=[_pr(labels=())]), ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK),
        (Board(prs=[_pr(labels=(REVIEW_LABEL, "blocked-failed"))]),
         ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK),
        (Board(checks=StatusCheckRollupRead(None, "transient_error")), ReviewReleaseRefusal.CHECKS_UNREADABLE),
        (Board(checks=StatusCheckRollupRead("PENDING")), ReviewReleaseRefusal.CHECKS_NOT_GREEN),
        (Board(checks=StatusCheckRollupRead("FAILURE")), ReviewReleaseRefusal.CHECKS_NOT_GREEN),
        (Board(checks=StatusCheckRollupRead(None)), ReviewReleaseRefusal.CHECKS_NOT_GREEN),
    ],
    ids=lambda value: value.value if isinstance(value, ReviewReleaseRefusal) else "",
)
def test_every_failed_precondition_refuses_typed_and_writes_nothing(
    board: Board, code: ReviewReleaseRefusal
) -> None:
    result = _refused(board)

    assert result.details["refusal"] == code.value
    # Only a review nothing withholds any more has met the remedy's goal.
    assert result.details["terminal_disposition_satisfied"] is (
        code is ReviewReleaseRefusal.REVIEW_NOT_WITHHELD
    )


def test_the_proposing_tech_leads_own_claim_does_not_refuse() -> None:
    """The investigation that proposed the release holds a claim on its focus
    while its completion applies it; only other work's claims refuse."""
    board = Board(claims=[
        _claim(PendingWorkKind.TECH_LEAD, session=SOURCE_SESSION, started_at=OBSERVED),
        # Another issue's claims are not this issue's.
        replace(_claim(PendingWorkKind.VALIDATION_RETRY), issue_number=ISSUE + 10),
    ])

    assert board.executor().apply(_action()).success


def test_a_failure_before_the_observation_is_the_block_the_tech_lead_diagnosed() -> None:
    board = Board(history=[_history("failed", BEFORE), _history("completed", AFTER)])

    assert board.executor().apply(_action()).success


def test_the_pr_that_carries_published_work_may_be_released() -> None:
    board = Board(holds=(PublishedReviewHold(ISSUE, PR, f"{ISSUE}-green", "rec", "a" * 40),))

    assert board.executor().apply(_action()).success


def test_a_prior_attempts_pr_is_ignored_like_review_discovery_ignores_it() -> None:
    current = _pr(7398, branch=f"{ISSUE}-current")
    board = Board(prs=[_pr(), current], branches={ISSUE: f"{ISSUE}-current"})

    result = board.executor().apply(_action())

    assert result.success and result.details["pr_number"] == 7398


@pytest.mark.parametrize("failing", [AddLabelAction, RemoveLabelAction])
def test_a_release_write_that_fails_fails_the_action_loudly(failing: type) -> None:
    board = Board(fail_writes=frozenset({failing}))

    result = board.executor().apply(_action())

    assert not result.success
    assert result.result_type.value == "failure"
    assert EventName.TECH_LEAD_ACTION_EXECUTED.value not in _event_names(board)
    if failing is AddLabelAction:
        # pr-pending was not confirmed, so blocked-failed was never touched.
        assert not any(isinstance(write, RemoveLabelAction) for write in board.writes)


def test_stale_reason_is_the_same_verification_without_writes() -> None:
    board = Board(checks=StatusCheckRollupRead("FAILURE"))
    executor = board.executor()

    reason = executor.stale_reason(ISSUE, OBSERVED, SOURCE_SESSION)

    assert reason is not None and reason.startswith("checks_not_green")
    assert board.writes == [] and board.events == []
    assert replace(board, checks=StatusCheckRollupRead("SUCCESS")).executor().stale_reason(ISSUE, OBSERVED, SOURCE_SESSION) is None


# -- the applier dispatches the action to this owner --------------------------


def test_the_applier_hands_the_action_to_the_wired_release_owner() -> None:
    from issue_orchestrator.control.action_applier import ActionApplier

    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
                            repository_host=MagicMock())
    owner = MagicMock()
    action = _action()
    owner.apply.return_value = ActionResult.ok(action, pr_number=PR)
    applier.release_withheld_review = owner

    result = applier.apply(action)

    assert result.success and result.details["pr_number"] == PR
    owner.apply.assert_called_once_with(action)


def test_an_unwired_release_owner_fails_loudly() -> None:
    from issue_orchestrator.control.action_applier import ActionApplier

    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
                            repository_host=MagicMock())

    result = applier.apply(_action())

    assert not result.success
    assert "release_withheld_review execution requested but no executor is wired" in (result.error or "")


def test_a_mandated_release_that_did_not_commit_is_routed_to_a_human_by_name() -> None:
    """A refused release in an investigation fails it; the operator surface
    names the release and its target instead of a generic act-level action."""
    from issue_orchestrator.control.tech_lead_completion_gate import RequiredActLevelOutcome
    from issue_orchestrator.control.tech_lead_reset_retry import build_required_act_level_failure_actions

    action = _action()
    outcome = RequiredActLevelOutcome(
        committed=False, failures=("checks_not_green: PR #7396 checks are FAILURE",),
        failed_actions=(action,),
    )

    label, comment = build_required_act_level_failure_actions(
        issue_number=ANCHOR, needs_human_label="needs-human", outcome=outcome,
        session_id="tech-lead-7395", runtime_minutes=4.0,
    )

    assert isinstance(label, AddLabelAction) and label.issue_number == ISSUE
    assert "- Action: `release_withheld_review`" in comment.comment  # type: ignore[attr-defined]
    assert f"withheld review of issue #{ISSUE}'s PR" in comment.comment  # type: ignore[attr-defined]


def test_an_issue_inside_the_configured_scope_is_released() -> None:
    board = Board(filter_label="io:e2e:run", issue=_issue("blocked-failed", "pr-pending", "io:e2e:run"))

    assert board.executor().apply(_action()).success


def _block_issue(board: Board) -> None:
    board.issue = _issue("blocked-failed", "pr-pending", "blocked", "io:e2e:run")


def _block_pr(board: Board) -> None:
    board.live = {PR: _pr(labels=(REVIEW_LABEL, "blocked-failed"))}


def _close_pr(board: Board) -> None:
    board.live = {PR: replace(_pr(), state="closed")}


def _leave_scope(board: Board) -> None:
    board.issue = _issue("blocked-failed", "pr-pending")  # lost the scope label


def _move_branch(board: Board) -> None:
    board.branches = {ISSUE: f"{ISSUE}-green"}
    board.live = {PR: _pr(branch=f"{ISSUE}-renamed")}


def _claim_work(board: Board) -> None:
    board.claims = [_claim(PendingWorkKind.VALIDATION_RETRY)]


@pytest.mark.parametrize("meanwhile", [_block_issue, _block_pr, _close_pr, _leave_scope, _move_branch, _claim_work])
def test_a_change_during_the_writes_withdraws_the_release_before_the_block_comes_off(
    meanwhile: "Callable[[Board], None]",
) -> None:
    """#7399 review r3/r4: the writes take time; every precondition is asked
    again, from fresh reads, right before blocked-failed would come off."""
    board = Board(
        during_route=meanwhile,
        filter_label="io:e2e:run",
        issue=_issue("blocked-failed", "pr-pending", "io:e2e:run"),
    )

    result = board.executor().apply(_action())

    assert not result.success
    assert result.details["status"] == "withdrawn"
    assert not any(isinstance(write, RemoveLabelAction) for write in board.writes)
    assert EventName.TECH_LEAD_ACTION_EXECUTED.value not in _event_names(board)
