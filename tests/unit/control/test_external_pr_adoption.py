"""Externally authored PRs in an integration effort (#9499), at the port boundaries.

The repository host is the in-memory fake (``MockGitHubAdapter``); the
standing-rulings owner is the real one over faked issue bodies. Discovery
(the real ``GitHubWorkflow``), the planner's step actions, the applier's
re-checks and writes, review discovery (the real ``PRScanner``), the review
completion's approval boundary (``record_reviewed_head``) and the integration
owner's merge gate are production code.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.awaiting_merge_reconciler import AwaitingMergeReconciler
from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
from issue_orchestrator.control.completion_reviewed_head import record_reviewed_head
from issue_orchestrator.control.external_pr_adoption import routing_gate, write_gate
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.integration_branch import (
    IntegrationBranchOwner,
    apply_integration_step,
    plan_integration_steps,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.pr_scanner import PRScanner, ReworkScan
from issue_orchestrator.domain.board_snapshot import BoardTechLeadWriteHealth
from issue_orchestrator.domain.external_pr_adoption import (
    AdoptExternalPullRequest,
    AdoptionRefusal,
    ReportRefusedExternalPullRequest,
    ReturnExternalPullRequestToReview,
    refusal_marker,
    reviewed_head_receipt,
)
from issue_orchestrator.domain.integration_branch import (
    BranchComparison,
    MergeIntoIntegration,
    UpdatePullRequestBranch,
)
from issue_orchestrator.domain.models import (
    ORCHESTRATOR_PR_MARKER,
    Issue,
    OrchestratorState,
    PendingReview,
    PendingRework,
    RequestedAction,
    SessionHistoryEntry,
)
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import IntegrationConfig
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.repository_host import RepositoryHostError
from tests.conftest import MockGitHubAdapter
from tests.standing_ruling_helpers import IssueBodies, a_ruling, body_with, rulings_owner

TIP = "1" * 40
MAIN = "2" * 40
HEAD = "a" * 40
PUSHED = "b" * 40
UPDATED = "c" * 40
EFFORT = "integrate:integration"


class _World:
    """One repo running an integration effort on ``integration``, with io's review lane."""

    def __init__(self) -> None:
        self.config = Config(repo="owner/repo")
        self.config.integration = IntegrationConfig(enabled=True)
        self.config.code_review_agent = "agent:reviewer"
        self.config.code_review_label = "needs-code-review"
        self.config.filtering.label = EFFORT
        self.labels = LabelManager(self.config)
        self.host = MockGitHubAdapter(repo="owner/repo")
        self.host.branches = {"main": MAIN, "integration": TIP}
        self.bodies = IssueBodies()
        self.rulings = rulings_owner(self.bodies)
        self.state = OrchestratorState()
        self.events = InMemoryEventSink()
        self.workflow = GitHubWorkflow(
            config=self.config, events=InMemoryEventSink(), repository_host=self.host,
            fact_gatherer=MagicMock(), pr_scanner=MagicMock(), label_sync=None,
            event_context=EventContext(), label_manager=self.labels, standing_rulings=self.rulings,
        )

    def issue(self, number: int, *labels: str) -> Issue:
        issue = Issue(number=number, title=f"Issue {number}", labels=list(labels), state="open")
        self.host.issues.append(issue)
        self.host.labels[number] = set(labels)  # GitHub's fresh label read
        self.state.cached_scope_issues.append(issue)
        return issue

    def effort_issue(self, number: int) -> Issue:
        return self.issue(number, "agent:backend", EFFORT)

    def relabel(self, number: int, *labels: str) -> None:
        """An issue's labels change on GitHub."""
        issue = self.host.get_issue(number)
        assert issue is not None
        issue.labels[:] = list(labels)
        self.host.labels[number] = set(labels)

    def pr(
        self, number: int, *, issue: int | None, base: str = "integration", body: str | None = None,
        labels: tuple[str, ...] = (), draft: bool = False, branch: str | None = None, head: str = HEAD,
    ) -> PRInfo:
        pr = PRInfo(
            number=number, title=f"Hand-made {number}", url=f"https://github.com/owner/repo/pull/{number}",
            branch=branch or (f"{issue}-hand-made" if issue is not None else f"hand-made-{number}"),
            body=body if body is not None else (f"Closes #{issue}" if issue is not None else "A change"),
            state="open", labels=list(labels), base_branch=base, head_sha=head, draft=draft,
            mergeable_state="clean", status_check_rollup="SUCCESS",
        )
        self.host.prs.setdefault(pr.branch, []).append(pr)
        self.host.labels[number] = set(labels)
        return pr

    def io_pr(self, number: int, *, issue: int, base: str = "integration") -> PRInfo:
        return self.pr(number, issue=issue, base=base, branch=f"{issue}-io-{number}",
                       body=f"Closes #{issue}\n\n*{ORCHESTRATOR_PR_MARKER}*")

    def discover(self, *, issue_branches: dict[int, str] | None = None) -> list:
        self.state.discovered_integration_steps.clear()
        self.workflow.adopt_external_prs(self.state, issue_branches=issue_branches or {})
        return list(self.state.discovered_integration_steps)

    def apply(self, step):
        [action] = plan_integration_steps([step])
        return apply_integration_step(action, host=self.host, labels=self.labels, rulings=self.rulings,
                                      external=write_gate(self.config, self.host, lambda: self.state),
                                      events=self.events)

    def adopt_round(self, **kwargs) -> list:
        steps = self.discover(**kwargs)
        for step in steps:
            self.apply(step)
        return steps

    def comments_on(self, number: int) -> list[str]:
        return [comment["body"] for comment in self.host.comments if comment["number"] == number]

    def review_queue(self) -> list[int]:
        scan = PRScanner(self.config, self.host, InMemoryEventSink(), label_manager=self.labels).scan_for_reviews(
            [], [], issue_branches={},
        )
        return [review.pr_number for review in scan.reviews]

    # -- io's review of an adopted PR, and what happens to its head --------------

    def approve(self, pr: PRInfo, issue_number: int, *, reviewed: str = HEAD) -> None:
        """io's reviewer approves, its worktree at *reviewed*: what the review's
        completion leaves - the receipt (the real approval boundary), the
        approval label, and the issue's awaiting-merge history."""
        errors: list[str] = []
        assert record_reviewed_head(
            RequestedAction.ADD_CODE_REVIEWED_LABEL, Path(f"/review-{pr.number}"), issue_number=issue_number,
            pr_number=pr.number, pr_adapter=self.host,
            git_adapter=SimpleNamespace(get_head_sha=lambda _worktree: reviewed), actions_taken=[], errors=errors,
        ), errors
        self.host.remove_label(pr.number, self.config.code_review_label or "")
        self.host.add_label(pr.number, self.labels.code_reviewed)
        self.state.session_history.append(SessionHistoryEntry(
            issue_number=issue_number, title=f"Issue {issue_number}", agent_type="agent:backend",
            status="completed", runtime_minutes=0, pr_url=pr.url,
        ))

    def current(self, head: str) -> None:
        """*head* already contains the integration tip."""
        self.host.comparisons[(TIP, head)] = BranchComparison(ahead_by=1, behind_by=0)

    def merge_pass(self):
        owner = IntegrationBranchOwner(
            config=self.config.integration, host=self.host, label_manager=self.labels,
            standing_rulings=self.rulings, clock=lambda: 1_000_000.0,
            external=routing_gate(self.config, self.host, self.state),
        )
        result = AwaitingMergeReconciler(
            self.host, label_manager=self.labels, repo="owner/repo", integration=owner,
        ).discover(self.state)
        return owner.discovered_steps(), result

    def merge_step(self, pr: PRInfo, issue_number: int, *, head: str = HEAD) -> MergeIntoIntegration:
        return MergeIntoIntegration(
            issue_number=issue_number, issue_key=f"owner/repo#{issue_number}", pr_number=pr.number,
            pr_url=pr.url, pr_title=pr.title, head_sha=head, integration_branch="integration",
            integration_tip=TIP, gate_label="code-reviewed",
        )


def _adopted_and_approved(world: _World, *, issue_number: int = 231, pr_number: int = 321) -> PRInfo:
    world.effort_issue(issue_number)
    pr = world.pr(pr_number, issue=issue_number)
    world.adopt_round()
    world.adopt_round()  # the scan after the write sees it adopted
    world.approve(pr, issue_number)
    world.current(HEAD)
    return pr


def _skipped(result) -> bool:
    return result.result_type is ActionResultType.SKIPPED  # type: ignore[attr-defined]


# -- adoption ------------------------------------------------------------------


def test_a_hand_opened_pr_in_the_effort_is_adopted_into_io_s_review_lane() -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231)

    [step] = world.discover()
    assert step == AdoptExternalPullRequest(
        issue_number=231, pr_number=321, pr_url=pr.url, pr_title=pr.title, integration_branch="integration",
        review_label="needs-code-review",
    )
    result = world.apply(step)

    assert result.result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]
    assert set(pr.labels) == {"needs-code-review", "origin:external"}
    # io's own review discovery now queues it, exactly as for io's own PRs.
    assert world.review_queue() == [321]
    # Adopted, it needs nothing more from this owner; the board knows it.
    assert world.discover() == []
    assert world.state.external_pull_requests.adopted_pr_numbers() == frozenset({321})
    applied = [e for e in world.events.events if e.event_type is EventName.INTEGRATION_STEP_APPLIED]
    assert [e.data["step"] for e in applied] == ["AdoptExternalPullRequest"]


def test_an_approval_io_did_not_give_never_stands_for_io_s_review() -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231, labels=("code-reviewed",))

    world.adopt_round()

    assert set(pr.labels) == {"needs-code-review", "origin:external"}
    assert (321, "code-reviewed") in world.host.remove_label_calls


@pytest.mark.parametrize("kind", ["io_authored", "draft", "delivery"])
def test_io_s_own_prs_drafts_and_the_delivery_pr_are_not_external_work(kind: str) -> None:
    world = _World()
    world.effort_issue(231)
    if kind == "io_authored":
        world.io_pr(321, issue=231)
    elif kind == "draft":
        world.pr(321, issue=231, draft=True)
    else:
        world.pr(321, issue=None, base="main", branch="integration", body="Deliver integration to main")
    world.host.get_issue = MagicMock(side_effect=AssertionError("no issue read"))  # type: ignore[method-assign]

    assert world.discover() == []
    assert world.host.add_label_calls == [] and world.host.comments == []


def test_a_candidate_past_the_first_hundred_open_prs_is_adopted() -> None:
    """F3: the scan reads EVERY open PR, never a first page that repeats forever."""
    world = _World()
    for number in range(1, 121):  # io's own work, older than the candidate
        world.io_pr(1000 + number, issue=5000 + number)
    world.effort_issue(231)
    world.pr(1500, issue=231)
    world.host.list_prs = MagicMock(side_effect=AssertionError("a page is not a scan"))  # type: ignore[method-assign]

    [step] = world.discover()

    assert isinstance(step, AdoptExternalPullRequest) and step.pr_number == 1500


# -- refusal -------------------------------------------------------------------


def _refusal(world: _World) -> ReportRefusedExternalPullRequest:
    [step] = world.discover()
    assert isinstance(step, ReportRefusedExternalPullRequest)
    return step


@pytest.mark.parametrize(
    ("setup", "refusal", "detail"),
    [
        ("no_reference", AdoptionRefusal.NO_ISSUE_REFERENCE, "references no issue"),
        ("not_labelled", AdoptionRefusal.ISSUE_NOT_IN_EFFORT, f"not labelled `{EFFORT}`"),
        ("closed", AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "not open in this engine's scope"),
        ("outside_milestone", AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "milestone"),
        ("no_agent", AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "no agent label"),
        ("wrong_base", AdoptionRefusal.WRONG_BASE, "targets `main`, but issue #231 integrates on `integration`"),
    ],
)
def test_a_pr_outside_the_effort_is_refused_told_once_and_never_adopted(
    setup: str, refusal: AdoptionRefusal, detail: str,
) -> None:
    world = _World()
    if setup == "no_reference":
        world.pr(321, issue=None)
    elif setup == "not_labelled":
        world.config.filtering.label = None  # the engine's scope alone is not the effort
        world.issue(231, "agent:backend")
        world.pr(321, issue=231)
    elif setup == "closed":
        world.effort_issue(231).state = "closed"
        world.pr(321, issue=231)
    elif setup == "outside_milestone":
        # In the effort by label, but outside the engine's milestone scope (review F2).
        world.config.filtering.milestone = "M1"
        world.effort_issue(231).milestone = "M2"
        world.pr(321, issue=231)
    elif setup == "no_agent":
        world.issue(231, EFFORT)
        world.pr(321, issue=231)
    else:
        world.effort_issue(231)
        world.pr(321, issue=231, base="main")

    step = _refusal(world)
    assert (step.refusal, step.pr_number) == (refusal, 321)
    assert detail in step.detail
    world.apply(step)

    [comment] = world.comments_on(321)
    assert comment.startswith(refusal_marker(refusal))
    assert "will never merge it" in comment
    assert world.host.add_label_calls == []
    # Told once: neither the next scan nor a replayed write tells it again.
    assert world.discover() == []
    world.apply(step)
    assert len(world.comments_on(321)) == 1


def test_a_pr_into_another_base_for_an_issue_outside_the_effort_is_not_io_s_business() -> None:
    """Every other open PR in the repository: no report, and no issue read."""
    world = _World()
    world.pr(321, issue=231, base="main")  # issue #231 is not one io holds
    world.host.get_issue = MagicMock(side_effect=AssertionError("no issue read"))  # type: ignore[method-assign]

    assert world.discover() == []
    assert world.state.external_pull_requests.reported == {}


def test_a_refusal_whose_reason_changes_is_told_again() -> None:
    world = _World()
    pr = world.pr(321, issue=None)
    world.apply(_refusal(world))
    world.effort_issue(231)
    pr.body = "Closes #231"  # now it references an issue ...
    pr.base_branch = "main"  # ... from the wrong base

    step = _refusal(world)
    world.apply(step)

    assert step.refusal is AdoptionRefusal.WRONG_BASE
    assert len(world.comments_on(321)) == 2


@pytest.mark.parametrize("where", ["branch", "history", "io_pr", "adopted_pr"])
def test_an_issue_io_already_holds_is_not_adopted_twice(where: str) -> None:
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231)
    branches = {231: "231-io-attempt"} if where == "branch" else {}
    if where == "history":
        world.state.session_history.append(SessionHistoryEntry(
            issue_number=231, title="Issue 231", agent_type="agent:backend", status="completed",
            runtime_minutes=3, pr_url="https://github.com/owner/repo/pull/300",
        ))
    elif where == "io_pr":
        world.io_pr(300, issue=231)
    elif where == "adopted_pr":
        world.pr(300, issue=231, branch="231-another-hand", labels=("origin:external",))

    steps = world.discover(issue_branches=branches)

    [step] = [s for s in steps if s.pr_number == 321]
    assert isinstance(step, ReportRefusedExternalPullRequest)
    assert step.refusal is AdoptionRefusal.IO_WORK_IN_FLIGHT


def test_an_unreadable_listing_adopts_refuses_and_forgets_nothing() -> None:
    """F3: a partial read never replaces the board's view."""
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231, labels=("origin:external", "needs-code-review"))
    world.discover()
    assert world.state.external_pull_requests.adopted_pr_numbers() == frozenset({321})
    world.state.external_pull_requests.reported[400] = AdoptionRefusal.NO_ISSUE_REFERENCE

    world.host.list_open_prs_complete = MagicMock(  # type: ignore[method-assign]
        side_effect=RepositoryHostError("502 listing pulls"),
    )

    assert world.discover() == []
    assert world.state.external_pull_requests.adopted_pr_numbers() == frozenset({321})
    assert world.state.external_pull_requests.reported == {400: AdoptionRefusal.NO_ISSUE_REFERENCE}


def test_without_integration_mode_nothing_is_listed() -> None:
    world = _World()
    world.config.integration = IntegrationConfig(enabled=False)
    world.host.list_open_prs_complete = MagicMock(side_effect=AssertionError("no listing"))  # type: ignore[method-assign]

    assert world.discover() == []


# -- the write re-judges THE admission rule on fresh reads -------------------------------


@pytest.mark.parametrize(
    "change",
    [
        "retargeted", "closed_pr", "reference_removed", "reference_moved", "issue_closed", "left_effort",
        "left_milestone", "agent_removed", "io_session_started", "io_pr_opened",
    ],
)
def test_an_adoption_is_not_written_when_its_facts_changed_since_planning(change: str) -> None:
    world = _World()
    world.effort_issue(231)
    world.effort_issue(232)
    pr = world.pr(321, issue=231)
    [step] = world.discover()
    if change == "retargeted":
        pr.base_branch = "main"
    elif change == "closed_pr":
        pr.state = "closed"
    elif change == "reference_removed":
        pr.body, pr.branch = "No issue any more", "hand-made-321"
    elif change == "reference_moved":
        pr.body, pr.branch = "Closes #232", "hand-made-321"
    elif change == "issue_closed":
        world.host.get_issue(231).state = "closed"  # type: ignore[union-attr]
    elif change == "left_effort":
        world.relabel(231, "agent:backend")
    elif change == "left_milestone":
        world.config.filtering.milestone = "M1"
    elif change == "agent_removed":
        world.relabel(231, EFFORT)
    elif change == "io_session_started":
        world.state.active_sessions.append(SimpleNamespace(  # type: ignore[arg-type]
            issue=SimpleNamespace(number=231), branch_name="231-io-attempt",
        ))
    else:
        world.io_pr(300, issue=231)

    result = world.apply(step)

    assert _skipped(result), result
    assert pr.labels == []


def test_a_refusal_fixed_before_its_write_is_not_told() -> None:
    world = _World()
    world.issue(231, "agent:backend")  # not in the effort yet
    world.config.filtering.label = None
    pr = world.pr(321, issue=231)
    step = _refusal(world)
    world.relabel(231, "agent:backend", EFFORT)  # the person fixed it meanwhile

    result = world.apply(step)

    assert _skipped(result)
    assert world.comments_on(321) == []
    assert pr.labels == []
    [adopt] = world.discover()
    assert isinstance(adopt, AdoptExternalPullRequest)


def test_a_refusal_whose_reason_changed_before_its_write_is_not_told_as_the_old_one() -> None:
    world = _World()
    pr = world.pr(321, issue=None)
    step = _refusal(world)
    world.effort_issue(231)
    pr.body, pr.base_branch = "Closes #231", "main"

    assert _skipped(world.apply(step))
    assert world.comments_on(321) == []


# -- fail closed at the merge ------------------------------------------------------


def test_an_adopted_approved_pr_is_merged_like_io_s_own() -> None:
    world = _World()
    pr = _adopted_and_approved(world)

    steps, _ = world.merge_pass()

    [merge] = steps
    assert isinstance(merge, MergeIntoIntegration) and merge.pr_number == 321
    assert world.apply(merge).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]
    [merged] = world.host.pr_merges
    assert (merged["branch"], merged["head_sha"]) == ("integration", HEAD)
    assert pr.state == "merged"


def test_io_s_review_receipt_outlives_a_restart() -> None:
    """The receipt lives on the PR: a fresh process (empty ledger) still merges."""
    world = _World()
    _adopted_and_approved(world)
    history = list(world.state.session_history)
    world.state = OrchestratorState(session_history=history)

    [merge], _ = world.merge_pass()

    assert isinstance(merge, MergeIntoIntegration)


@pytest.mark.parametrize("state", ["not_adopted", "left_effort"])
def test_io_never_merges_reworks_or_updates_an_unadmitted_external_pr(state: str) -> None:
    """A review label applied by hand cannot carry an unadopted PR into the branch."""
    world = _World()
    world.effort_issue(231)
    labels = ("origin:external",) if state == "left_effort" else ()  # adopted once, then the issue left
    pr = world.pr(321, issue=231, labels=labels)
    if state == "left_effort":
        world.relabel(231, "agent:backend")
    world.host.add_label(321, world.labels.code_reviewed)
    world.state.session_history.append(SessionHistoryEntry(
        issue_number=231, title="Issue 231", agent_type="agent:backend", status="completed",
        runtime_minutes=0, pr_url=pr.url,
    ))
    world.current(HEAD)
    pr.mergeable_state = "dirty"  # a conflict would otherwise send it to an io rework

    steps, result = world.merge_pass()

    assert steps == [] and result.reworks == () and result.escalations == ()
    # The write refuses it too, on fresh reads.
    write = world.apply(world.merge_step(pr, 231))
    assert _skipped(write)
    assert "not_admitted" in write.details["skip_reason"]  # type: ignore[attr-defined]
    assert world.host.pr_merges == []


@pytest.mark.parametrize("link", ["removed", "moved"])
@pytest.mark.parametrize("lane", ["rework", "update", "merge"])
def test_an_adopted_pr_whose_issue_link_changes_is_never_reworked_updated_or_merged(link: str, lane: str) -> None:
    """Review F2: the merge side re-asks the exact PR-issue link, not the old history's issue."""
    world = _World()
    world.effort_issue(232)
    pr = _adopted_and_approved(world)
    pr.body, pr.branch = ("No issue", "hand-made-321") if link == "removed" else ("Closes #232", "hand-made-321")
    if lane == "rework":
        pr.mergeable_state = "dirty"
    elif lane == "update":
        world.host.comparisons[(TIP, HEAD)] = BranchComparison(ahead_by=1, behind_by=2)

    steps, result = world.merge_pass()

    assert steps == [] and result.reworks == () and result.escalations == ()
    for step in (world.merge_step(pr, 231), UpdatePullRequestBranch(
        issue_number=231, pr_number=321, head_sha=HEAD, integration_tip=TIP, integration_branch="integration",
        gate_label="code-reviewed",
    )):
        assert _skipped(world.apply(step))
    assert world.host.pr_merges == [] and world.host.pr_branch_updates == []


@pytest.mark.parametrize(
    ("link", "rework_issue", "reworked"),
    [
        ("kept", 231, True),
        ("moved", 231, False),  # the scan's stale issue: the PR now references #232
        ("moved", 232, False),  # the issue it now references is outside the effort
        ("removed", 231, False),
    ],
)
def test_a_review_s_rework_of_an_adopted_pr_needs_the_pr_still_admitted(
    link: str, rework_issue: int, reworked: bool,
) -> None:
    """Review F2: the needs-rework lane re-asks THE admission rule too."""
    world = _World()
    world.issue(232, "agent:backend")  # in scope, not in the effort
    world.config.filtering.label = None
    pr = _adopted_and_approved(world)
    if link == "moved":
        pr.body, pr.branch = "Closes #232", "hand-made-321"
    elif link == "removed":
        pr.body, pr.branch = "No issue", "hand-made-321"
    world.workflow.pr_scanner.scan_for_reworks.return_value = ReworkScan(  # type: ignore[attr-defined]
        reworks=[PendingRework(issue_key=world.host.create_issue_key(rework_issue), agent_type="agent:backend",
                               issue_number=rework_issue, pr_number=321)],
        escalations=[], blocked=[],
    )

    world.workflow.scan_needs_rework_prs(world.state)

    assert [r.pr_number for r in world.state.discovered_reworks] == ([321] if reworked else [])


def test_an_adopted_pr_whose_issue_carries_a_ruling_is_handed_to_a_person() -> None:
    world = _World()
    world.bodies.bodies[231] = body_with(a_ruling())
    _adopted_and_approved(world)

    steps, result = world.merge_pass()

    assert steps == []
    [escalation] = result.escalations
    assert (escalation.kind, escalation.pr_number) == ("integration_ruling_check", 321)


# -- reviewed-head custody (review F1) -----------------------------------------------


def _returned_to_review(world: _World, pr: PRInfo, head: str) -> None:
    """The pass sends the PR back to io's review, and the write does it."""
    steps, result = world.merge_pass()
    assert result.reworks == () and result.escalations == ()
    assert steps == [ReturnExternalPullRequestToReview(
        issue_number=231, pr_number=pr.number, head_sha=head, integration_branch="integration",
        review_label="needs-code-review",
    )]
    assert world.apply(steps[0]).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]
    assert world.labels.code_reviewed not in pr.labels and "needs-code-review" in pr.labels
    assert world.review_queue() == [pr.number]  # io's review reads the new head
    assert world.host.pr_merges == []


def test_a_push_after_io_s_approval_is_not_merged_until_io_reviews_it() -> None:
    world = _World()
    pr = _adopted_and_approved(world)
    pr.head_sha = PUSHED  # the author pushes again; its checks pass, it contains the tip
    world.current(PUSHED)

    # Even a merge of the new head written by hand is refused at the write.
    write = world.apply(world.merge_step(pr, 231, head=PUSHED))
    assert _skipped(write) and "unreviewed_head" in write.details["skip_reason"]  # type: ignore[attr-defined]
    _returned_to_review(world, pr, PUSHED)

    # io reviews the new head: now it merges, at exactly that head.
    world.approve(pr, 231, reviewed=PUSHED)
    [merge], _ = world.merge_pass()
    assert isinstance(merge, MergeIntoIntegration) and merge.head_sha == PUSHED


def test_a_push_racing_io_s_review_completion_is_not_merged() -> None:
    """The reviewer read HEAD; the author pushed before the approval labels were written."""
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231)
    world.adopt_round()
    world.adopt_round()
    pr.head_sha = PUSHED
    world.current(PUSHED)

    world.approve(pr, 231, reviewed=HEAD)  # the review's worktree was at HEAD

    assert world.comments_on(321)[-1] == reviewed_head_receipt(321, HEAD)
    _returned_to_review(world, pr, PUSHED)


def test_io_s_own_update_onto_the_tip_does_not_carry_the_approval() -> None:
    """The mechanical update makes a new head no review read: io updates the
    reviewed head, then reviews the updated one before merging it."""
    world = _World()
    pr = _adopted_and_approved(world)
    world.host.comparisons[(TIP, HEAD)] = BranchComparison(ahead_by=1, behind_by=2)

    [update], _ = world.merge_pass()
    assert isinstance(update, UpdatePullRequestBranch) and update.head_sha == HEAD
    assert world.apply(update).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]
    assert world.host.pr_branch_updates == [(321, HEAD)]
    pr.head_sha = UPDATED  # GitHub merged the tip into the branch
    world.current(UPDATED)

    _returned_to_review(world, pr, UPDATED)


def test_a_receipt_anyone_else_writes_is_not_io_s_review() -> None:
    world = _World()
    pr = _adopted_and_approved(world)
    pr.head_sha = PUSHED
    world.current(PUSHED)
    world.host.comments.append({"number": 321, "body": reviewed_head_receipt(321, PUSHED), "author": "the-author"})

    _returned_to_review(world, pr, PUSHED)


def test_a_return_to_review_is_not_written_once_io_reviewed_that_head() -> None:
    world = _World()
    pr = _adopted_and_approved(world)
    pr.head_sha = PUSHED
    world.current(PUSHED)
    [step], _ = world.merge_pass()
    world.approve(pr, 231, reviewed=PUSHED)  # io's review of PUSHED completed meanwhile

    assert _skipped(world.apply(step))
    assert world.labels.code_reviewed in pr.labels


def test_an_unwritable_receipt_halts_the_approval() -> None:
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231, labels=("origin:external", "needs-code-review"))
    world.host.add_comment = MagicMock(side_effect=RepositoryHostError("502"))  # type: ignore[method-assign]
    errors: list[str] = []

    proceed = record_reviewed_head(
        RequestedAction.ADD_CODE_REVIEWED_LABEL, Path("/review-321"), issue_number=231, pr_number=321,
        pr_adapter=world.host, git_adapter=SimpleNamespace(get_head_sha=lambda _worktree: HEAD),
        actions_taken=[], errors=errors,
    )

    assert proceed is False
    assert "receipt for PR #321 not written" in errors[0]


def test_io_s_own_pr_approval_leaves_no_receipt() -> None:
    world = _World()
    world.effort_issue(231)
    world.io_pr(321, issue=231)

    assert record_reviewed_head(
        RequestedAction.ADD_CODE_REVIEWED_LABEL, Path("/review-321"), issue_number=231, pr_number=321,
        pr_adapter=world.host, git_adapter=SimpleNamespace(get_head_sha=lambda _worktree: HEAD),
        actions_taken=[], errors=[],
    )
    assert world.comments_on(321) == []


# -- the board ---------------------------------------------------------------------


def _builder() -> BoardSnapshotBuilder:
    return BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: [], log_tail_provider=lambda n: [],
        case_file_reader=lambda: (), shipped_fix_reader=lambda limit: (), e2e_health_reader=lambda now: None,
        tech_lead_write_health_reader=lambda now: BoardTechLeadWriteHealth(
            verdict="idle", is_alarm=False, reason="test", stale_after_hours=48.0,
        ),
        session_activity_reader=lambda session: None, clock=lambda: datetime(2026, 10, 13, 12, 0),
    )


def test_the_board_names_adopted_work_s_origin_in_io_s_own_queues() -> None:
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231)
    world.adopt_round()
    world.adopt_round()  # the scan after the write sees the PR adopted
    issue_key = world.host.create_issue_key(231)
    world.state.pending_reviews.extend([
        PendingReview(issue_key=issue_key, pr_number=321, pr_url="u321", branch_name="231-hand-made",
                      _issue_number=231),
        PendingReview(issue_key=issue_key, pr_number=330, pr_url="u330", branch_name="231-io",
                      _issue_number=231),
    ])
    world.state.pending_reworks.append(PendingRework(
        issue_key=issue_key, agent_type="agent:backend", rework_cycle=1, issue_number=231, pr_number=321,
    ))

    queues = _builder().build(world.state).queues

    details = [entry.detail for entry in queues]
    assert details[0].endswith("[origin:external]")
    assert "[origin:external]" not in details[1]
    assert "PR #321 [origin:external]" in details[2]


def test_the_board_keeps_an_adopted_pr_s_origin_however_many_prs_are_open() -> None:
    """F3: an adopted PR older than the newest hundred keeps its origin on the board."""
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231, labels=("origin:external", "needs-code-review"))
    for number in range(1, 121):  # newer work, io's own
        world.io_pr(2000 + number, issue=6000 + number)
    world.state.pending_reviews.append(PendingReview(
        issue_key=world.host.create_issue_key(231), pr_number=321, pr_url="u321", branch_name="231-hand-made",
        _issue_number=231,
    ))

    world.discover()
    [entry] = _builder().build(world.state).queues

    assert entry.detail.endswith("[origin:external]")
