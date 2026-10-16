"""Externally authored PRs in an integration effort (#9499), at the port boundaries.

The repository host is the in-memory fake (``MockGitHubAdapter``); the
standing-rulings owner is the real one over faked issue bodies. Discovery
(the real ``GitHubWorkflow``), the planner's step actions, the applier's
re-checks and writes, review discovery (the real ``PRScanner``), the review
completion's approval boundary (``record_reviewed_head``) and the integration
owner's merge gate are production code.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.awaiting_merge_reconciler import AwaitingMergeReconciler
from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
from issue_orchestrator.control.completion_reviewed_head import approve_pull_request
from issue_orchestrator.control.external_pr_adoption import routing_gate, write_gate
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.integration_branch import (
    IntegrationBranchOwner,
    apply_integration_step,
    plan_integration_steps,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.external_pr_launch import launch_gate
from issue_orchestrator.control.pr_scanner import PRScanner, ReviewScan, ReworkScan
from issue_orchestrator.control.session_launch_types import LaunchDisposition
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.board_snapshot import BoardTechLeadWriteHealth
from issue_orchestrator.control.actions import QueueReviewAction, QueueReworkAction
from issue_orchestrator.domain.external_pr_adoption import (
    AdoptExternalPullRequest,
    AdoptionRefusal,
    LaunchAuthority,
    LaunchRequest,
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
    AgentConfig,
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
from issue_orchestrator.ports.pull_request_tracker import PRInfo, PullRequestAuthor
from issue_orchestrator.ports.repository_host import RepositoryHostError
from tests.conftest import HAND_AUTHOR, IO_AUTHOR, MockGitHubAdapter
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
        self.config.agents = {"agent:backend": AgentConfig(prompt_path=Path("backend.md"))}
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
        author: PullRequestAuthor = HAND_AUTHOR,
    ) -> PRInfo:
        pr = PRInfo(
            number=number, title=f"Hand-made {number}", url=f"https://github.com/owner/repo/pull/{number}",
            branch=branch or (f"{issue}-hand-made" if issue is not None else f"hand-made-{number}"),
            body=body if body is not None else (f"Closes #{issue}" if issue is not None else "A change"),
            state="open", labels=list(labels), base_branch=base, head_sha=head, draft=draft,
            mergeable_state="clean", status_check_rollup="SUCCESS", author=author,
        )
        self.host.prs.setdefault(pr.branch, []).append(pr)
        self.host.labels[number] = set(labels)
        return pr

    def io_pr(self, number: int, *, issue: int, base: str = "integration") -> PRInfo:
        """A PR io opened: io's account is its author, its body carries io's marker."""
        return self.pr(number, issue=issue, base=base, branch=f"{issue}-io-{number}",
                       body=f"Closes #{issue}\n\n*{ORCHESTRATOR_PR_MARKER}*", author=IO_AUTHOR)

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
        completion leaves - the receipt and the approval label (the real
        approval boundary), the review lane retired, and the issue's
        awaiting-merge history."""
        errors: list[str] = []
        assert self.approval(pr, issue_number, reviewed=reviewed, errors=errors) is True, errors
        self.host.remove_label(pr.number, self.config.code_review_label or "")
        self.state.session_history.append(SessionHistoryEntry(
            issue_number=issue_number, title=f"Issue {issue_number}", agent_type="agent:backend",
            status="completed", runtime_minutes=0, pr_url=pr.url,
        ))

    def approval(self, pr: PRInfo, issue_number: int, *, reviewed: str = HEAD, errors: list[str]) -> bool | None:
        return approve_pull_request(
            RequestedAction.ADD_CODE_REVIEWED_LABEL, Path(f"/review-{pr.number}"), issue_number=issue_number,
            pr_number=pr.number, pr_adapter=self.host, git_adapter=SimpleNamespace(get_head_sha=lambda _worktree: reviewed),
            labels=self.host, approval_label=self.labels.code_reviewed, actions_taken=[], errors=errors,
        )

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


# -- provenance: io's own PRs are the ones io opened (review F1) --------------------


def _marker_pasting(world: _World, *, labels: tuple[str, ...] = ()) -> PRInfo:
    """A hand-opened PR whose author pasted io's marker into its body from the start."""
    world.effort_issue(231)
    return world.pr(321, issue=231, body=f"Closes #231\n\n*{ORCHESTRATOR_PR_MARKER}*", labels=labels)


def test_a_hand_opened_pr_pasting_io_s_marker_is_adopted_into_exact_head_custody() -> None:
    """The body is its author's to write: only io's account as the PR's author
    makes it io's own. Pasted, the marker buys nothing - the PR is adopted,
    approved only at the head io's review read, and a later push is not merged."""
    world = _World()
    pr = _marker_pasting(world, labels=("code-reviewed",))  # an approval io never gave, too

    [step] = world.discover()
    assert isinstance(step, AdoptExternalPullRequest) and step.pr_number == 321
    world.apply(step)
    world.adopt_round()
    assert set(pr.labels) == {"needs-code-review", "origin:external"}
    assert world.review_queue() == [321]

    world.approve(pr, 231)  # io's review read HEAD
    assert world.comments_on(321) == [reviewed_head_receipt(321, HEAD)]
    pr.head_sha = PUSHED  # its author pushes again after the approval
    world.current(PUSHED)

    write = world.apply(world.merge_step(pr, 231, head=PUSHED))
    assert _skipped(write) and "unreviewed_head" in write.details["skip_reason"]  # type: ignore[attr-defined]
    _returned_to_review(world, pr, PUSHED)


def test_a_marker_pasting_pr_approved_before_adoption_is_never_merged() -> None:
    """The attack review F1 named: approved in io's ordinary lane as if io had
    opened it, then pushed. Not io's own, it is not admitted until adopted -
    and the adoption drops the approval it was given before."""
    world = _World()
    pr = _marker_pasting(world, labels=("needs-code-review",))
    world.approve(pr, 231)  # no receipt: nothing adopted it yet
    pr.head_sha = PUSHED
    world.current(PUSHED)

    steps, result = world.merge_pass()
    assert steps == [] and result.reworks == ()
    write = world.apply(world.merge_step(pr, 231, head=PUSHED))
    assert _skipped(write) and "not_admitted" in write.details["skip_reason"]  # type: ignore[attr-defined]

    world.adopt_round()
    assert set(pr.labels) == {"needs-code-review", "origin:external"}  # back in io's review lane
    assert world.merge_pass()[0] == [] and world.host.pr_merges == []


def test_a_pr_io_opened_without_its_marker_is_external_work() -> None:
    """The marker is the hint that lets every other PR skip the account read:
    a PR io's account opened whose body lost it is judged as anyone's."""
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231, author=IO_AUTHOR)

    [step] = world.discover()

    assert isinstance(step, AdoptExternalPullRequest)


def test_a_scan_whose_provenance_cannot_be_read_adopts_and_refuses_nothing() -> None:
    world = _World()
    world.effort_issue(231)
    world.io_pr(320, issue=231)
    world.effort_issue(232)
    world.pr(321, issue=232)
    world.host.integration_failures["engine_account_id"] = RepositoryHostError("502 /user")

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
        ("unknown_agent", AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "`agent:unknown` names no agent this engine runs"),
        ("wrong_base", AdoptionRefusal.WRONG_BASE, "targets `main`, but issue #231 integrates on `integration`"),
        # Review r3 F6: a PR for several issues, however the lanes would pick one.
        ("branch_and_body_disagree", AdoptionRefusal.AMBIGUOUS_ISSUE_REFERENCE, "references issues #231 and #232"),
        ("two_closing_references", AdoptionRefusal.AMBIGUOUS_ISSUE_REFERENCE, "references issues #231 and #232"),
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
    elif setup == "unknown_agent":
        # An agent label io has no agent for: io could never rework the PR (review r2 F1).
        world.issue(231, "agent:unknown", EFFORT)
        world.pr(321, issue=231)
    elif setup == "branch_and_body_disagree":
        world.effort_issue(231)
        world.effort_issue(232)
        world.pr(321, issue=231, branch="231-feature", body="Closes #232")
    elif setup == "two_closing_references":
        world.effort_issue(231)
        world.effort_issue(232)
        world.pr(321, issue=None, branch="hand-made", body="Closes #231\nCloses #232")
    else:
        world.effort_issue(231)
        world.pr(321, issue=231, base="main")

    step = _refusal(world)
    assert (step.refusal, step.pr_number) == (refusal, 321)
    assert detail in step.detail
    world.apply(step)

    [comment] = world.comments_on(321)
    assert comment.startswith(refusal_marker(step))
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
    world.state.external_pull_requests.reported[400] = "<!-- io:external-pr-refused reason=x id=y -->"

    world.host.list_open_prs_complete = MagicMock(  # type: ignore[method-assign]
        side_effect=RepositoryHostError("502 listing pulls"),
    )

    assert world.discover() == []
    assert world.state.external_pull_requests.adopted_pr_numbers() == frozenset({321})
    assert world.state.external_pull_requests.reported == {400: "<!-- io:external-pr-refused reason=x id=y -->"}


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


@pytest.mark.parametrize("state", ["not_adopted", "left_effort", "unknown_agent"])
def test_io_never_merges_reworks_or_updates_an_unadmitted_external_pr(state: str) -> None:
    """A review label applied by hand cannot carry an unadopted PR into the branch;
    an adopted PR whose issue left the effort, or now names an agent io does
    not run (review r2 F1), is not reworked, updated or merged either."""
    world = _World()
    world.effort_issue(231)
    labels = () if state == "not_adopted" else ("origin:external",)  # adopted once, then the issue changed
    pr = world.pr(321, issue=231, labels=labels)
    if state == "left_effort":
        world.relabel(231, "agent:backend")
    elif state == "unknown_agent":
        world.relabel(231, "agent:unknown", EFFORT)
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


@pytest.mark.parametrize("link", ["removed", "moved", "ambiguous"])
@pytest.mark.parametrize("lane", ["rework", "update", "merge"])
def test_an_adopted_pr_whose_issue_link_changes_is_never_reworked_updated_or_merged(link: str, lane: str) -> None:
    """Review F2: the merge side re-asks the exact PR-issue link, not the old history's issue."""
    world = _World()
    world.effort_issue(232)
    pr = _adopted_and_approved(world)
    pr.body, pr.branch = {
        "removed": ("No issue", "hand-made-321"),
        "moved": ("Closes #232", "hand-made-321"),
        "ambiguous": ("Closes #231\nCloses #232", "hand-made-321"),
    }[link]
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
        ("unknown_agent", 231, False),  # its issue now names an agent io does not run (review r2 F1)
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
    elif link == "unknown_agent":
        world.relabel(231, "agent:unknown", EFFORT)
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
    pr = world.pr(321, issue=231, labels=("origin:external", "needs-code-review"))
    world.host.add_comment = MagicMock(side_effect=RepositoryHostError("502"))  # type: ignore[method-assign]
    errors: list[str] = []

    assert world.approval(pr, 231, errors=errors) is False
    assert "PR #321's approval not written" in errors[0]
    assert set(pr.labels) == {"origin:external", "needs-code-review"}  # still in io's review lane


def test_io_s_own_pr_approval_leaves_no_receipt() -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.io_pr(321, issue=231)

    assert world.approval(pr, 231, errors=[]) is True
    assert world.comments_on(321) == []
    assert world.labels.code_reviewed in pr.labels


# -- the launch boundary re-asks THE admission rule (review r3 F5) ----------------------


QUEUED_UNDER = LaunchAuthority(effort="integration", issue_number=231, pr_number=321, agent="agent:backend")


def _queue(world: _World, kind: SessionKind) -> LaunchRequest:
    """Discovery queues the review or rework of PR #321, admitted then; the
    planner's action builds the queued item: the launch request it carries."""
    if kind is SessionKind.REVIEW:
        world.workflow.pr_scanner.scan_for_reviews.return_value = ReviewScan(  # type: ignore[attr-defined]
            reviews=[PendingReview(issue_key=world.host.create_issue_key(231), pr_number=321, pr_url="u321",
                                   branch_name="231-hand-made", _issue_number=231)],
            blocked=[],
        )
        world.workflow.scan_needs_code_review_prs(world.state)
        [found] = world.state.discovered_reviews
        review = QueueReviewAction(issue_number=found.issue_number, pr_number=found.pr_number, pr_url=found.pr_url,
                                   branch_name=found.branch_name, agent_label=found.agent_label,
                                   authority=found.authority).pending(world.host.create_issue_key(231))
        # The admitted issue's agent and the whole authority, as production queues them.
        assert (review.agent_label, review.authority) == ("agent:backend", QUEUED_UNDER)
        return LaunchRequest(kind, review.issue_number, review.pr_number, review.authority, review.agent_label)
    world.workflow.pr_scanner.scan_for_reworks.return_value = ReworkScan(  # type: ignore[attr-defined]
        reworks=[PendingRework(issue_key=world.host.create_issue_key(231), agent_type="agent:backend",
                               issue_number=231, pr_number=321)],
        escalations=[], blocked=[],
    )
    world.workflow.scan_needs_rework_prs(world.state)
    [found_rework] = world.state.discovered_reworks
    rework = QueueReworkAction(issue_number=found_rework.issue_number, pr_number=found_rework.pr_number,
                               authority=found_rework.authority).pending(world.host.create_issue_key(231), "agent:x")
    assert (rework.agent_type, rework.authority) == ("agent:backend", QUEUED_UNDER)
    return LaunchRequest(kind, rework.issue_number, rework.pr_number or 0, rework.authority, rework.agent_type)


def _no_longer_admitted(world: _World, pr: PRInfo, change: str) -> None:
    """Admission facts change while the work waits for a slot."""
    if change == "unknown_agent":
        world.relabel(231, "agent:unknown", EFFORT)
    elif change == "left_effort":
        world.relabel(231, "agent:backend")
    elif change == "issue_closed":
        world.host.get_issue(231).state = "closed"  # type: ignore[union-attr]
    elif change == "link_moved":
        pr.body, pr.branch = "Closes #232", "hand-made-321"
    elif change == "ambiguous":
        pr.body, pr.branch = "Closes #231\nCloses #232", "hand-made-321"
    elif change == "retargeted":
        pr.base_branch = "main"
    elif change == "origin_lost":
        world.host.remove_label(321, "origin:external")
    elif change == "origin_lost_and_retargeted":
        # Review r4 F5: together, the PR's current facts no longer say the
        # effort governs it - only the queued authority does.
        world.host.remove_label(321, "origin:external")
        pr.base_branch = "main"
    elif change == "origin_lost_and_io_marker":
        world.host.remove_label(321, "origin:external")
        pr.body = f"Closes #231\n\n*{ORCHESTRATOR_PR_MARKER}*"
    elif change == "agent_changed":
        # Another configured agent: the queued work would run as the old one.
        world.config.agents["agent:frontend"] = AgentConfig(prompt_path=Path("frontend.md"))
        world.relabel(231, "agent:frontend", EFFORT)
    else:
        world.state.active_sessions.append(SimpleNamespace(  # type: ignore[arg-type]
            issue=SimpleNamespace(number=231), branch_name="231-io-attempt",
        ))


@pytest.mark.parametrize("kind", [SessionKind.REVIEW, SessionKind.REWORK])
@pytest.mark.parametrize(
    "change",
    ["unknown_agent", "left_effort", "issue_closed", "link_moved", "ambiguous", "retargeted", "origin_lost",
     "origin_lost_and_retargeted", "origin_lost_and_io_marker", "agent_changed", "io_session_started"],
)
def test_queued_work_on_an_adopted_pr_is_withdrawn_at_launch_once_the_pr_is_no_longer_admitted(
    kind: SessionKind, change: str,
) -> None:
    world = _World()
    world.effort_issue(232)
    pr = _adopted_and_approved(world)
    request = _queue(world, kind)
    gate = launch_gate(world.config, world.host, lambda: world.state)
    assert gate.refuse(request) is None

    _no_longer_admitted(world, pr, change)
    refused = gate.refuse(request)

    assert refused is not None and not refused.success
    assert refused.disposition is LaunchDisposition.WITHDRAWN, refused.reason


def test_without_its_queued_authority_the_same_edits_would_look_ungoverned() -> None:
    """Why the queued item carries the authority: the PR's facts alone (origin
    label dropped, base moved to main) say the effort does not govern it."""
    world = _World()
    pr = _adopted_and_approved(world)
    request = _queue(world, SessionKind.REWORK)
    _no_longer_admitted(world, pr, "origin_lost_and_retargeted")

    gate = launch_gate(world.config, world.host, lambda: world.state)

    assert gate.refuse(replace(request, authority=None)) is None
    assert gate.refuse(request) is not None


def test_work_on_an_adopted_pr_never_runs_unowned_once_its_effort_is_off() -> None:
    """Review r5/r6 F9: a restart with integration mode off never launches work
    queued under an effort's authority, nor work on a PR an effort adopted -
    even if a scan re-queued it without one."""
    world = _World()
    pr = _adopted_and_approved(world)
    request = _queue(world, SessionKind.REWORK)
    world.config.integration = IntegrationConfig(enabled=False)
    gate = launch_gate(world.config, world.host, lambda: world.state)

    for queued in (request, replace(request, authority=None)):
        refused = gate.refuse(queued)
        assert refused is not None and refused.disposition is LaunchDisposition.WITHDRAWN
    # A PR no effort adopted is no effort's business: it runs as without one.
    world.host.remove_label(pr.number, "origin:external")
    assert gate.refuse(replace(request, authority=None)) is None


def test_a_launch_whose_admission_cannot_be_read_is_retried_not_withdrawn() -> None:
    world = _World()
    _adopted_and_approved(world)
    request = _queue(world, SessionKind.REWORK)
    world.host.get_issue_labels_fresh = MagicMock(  # type: ignore[method-assign]
        side_effect=RepositoryHostError("502 labels"),
    )

    refused = launch_gate(world.config, world.host, lambda: world.state).refuse(request)

    assert refused is not None and refused.disposition is LaunchDisposition.RETRYABLE_FAILURE


@pytest.mark.parametrize("pr_kind", ["io_own", "external_elsewhere"])
def test_work_on_a_pr_the_effort_does_not_govern_launches_as_without_it(pr_kind: str) -> None:
    world = _World()
    world.effort_issue(231)
    if pr_kind == "io_own":
        world.io_pr(321, issue=231)
    else:
        world.pr(321, issue=231, base="main")  # a person's PR into main: not the effort's business

    assert launch_gate(world.config, world.host, lambda: world.state).refuse(
        LaunchRequest(SessionKind.REVIEW, 231, 321, None, None),
    ) is None


def test_a_pr_found_at_launch_that_the_effort_governs_is_not_reworked() -> None:
    """Review r5 F8: a rework queued without a PR never falls back onto an adopted one."""
    world = _World()
    _adopted_and_approved(world)

    refused = launch_gate(world.config, world.host, lambda: world.state).refuse(
        LaunchRequest(SessionKind.REWORK, 231, 321, None, "agent:backend", resolved_at_launch=True),
    )

    assert refused is not None and refused.disposition is LaunchDisposition.WITHDRAWN


def test_a_review_of_an_adopted_pr_no_longer_admitted_is_not_queued() -> None:
    world = _World()
    _adopted_and_approved(world)
    world.relabel(231, "agent:backend")  # left the effort
    world.workflow.pr_scanner.scan_for_reviews.return_value = ReviewScan(  # type: ignore[attr-defined]
        reviews=[PendingReview(issue_key=world.host.create_issue_key(231), pr_number=321, pr_url="u321",
                               branch_name="231-hand-made", _issue_number=231)],
        blocked=[],
    )

    world.workflow.scan_needs_code_review_prs(world.state)

    assert world.state.discovered_reviews == []


# -- writes that fail part-way converge (review r2 F2, F3) -----------------------------


def _fail_once(world: _World, method: str, failing: str) -> None:
    """The host's *method* fails once, for the label or comment *failing* names."""
    original = getattr(world.host, method)
    failed: list[str] = []

    def flaky(number: int, value: str) -> object:
        if failing in value and not failed:
            failed.append(value)
            raise RepositoryHostError(f"502 {method} {value}")
        return original(number, value)

    setattr(world.host, method, flaky)


def _failed(result) -> bool:
    return result.result_type is ActionResultType.FAILURE  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("method", "label"),
    [("remove_label", "tech-lead-reviewed"), ("add_label", "needs-code-review"), ("remove_label", "code-reviewed")],
)
def test_a_return_to_review_that_fails_part_way_is_planned_again_and_converges(method: str, label: str) -> None:
    world = _World()
    pr = _adopted_and_approved(world)
    world.host.add_label(321, world.labels.tech_lead_reviewed)
    pr.head_sha = PUSHED
    world.current(PUSHED)
    [step], _ = world.merge_pass()
    _fail_once(world, method, label)

    assert _failed(world.apply(step))
    # Every partial state still carries code-reviewed, so the next pass plans
    # the same return: never a PR outside every lane, nor a stale approval.
    [again], _ = world.merge_pass()
    assert again == step
    assert world.apply(again).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]

    assert set(pr.labels) == {"origin:external", "needs-code-review"}
    assert world.review_queue() == [321]


@pytest.mark.parametrize(
    ("method", "label"),
    [("remove_label", "code-reviewed"), ("add_label", "needs-code-review"), ("add_label", "origin:external")],
)
def test_an_adoption_that_fails_part_way_is_planned_again_and_converges(method: str, label: str) -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231, labels=("code-reviewed",))
    [step] = world.discover()
    _fail_once(world, method, label)

    assert _failed(world.apply(step))
    [again] = world.discover()  # no origin label yet: still a candidate
    assert again == step
    assert world.apply(again).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]

    assert set(pr.labels) == {"needs-code-review", "origin:external"}


def test_a_refusal_whose_comment_failed_is_told_on_the_next_scan() -> None:
    world = _World()
    world.pr(321, issue=None)
    step = _refusal(world)
    _fail_once(world, "add_comment", "io:external-pr-refused")

    assert _failed(world.apply(step))
    assert world.comments_on(321) == []
    # Planned is not told: the next scan plans it again, and only a report seen
    # on the PR is acknowledged.
    assert _refusal(world) == step
    world.apply(step)
    assert len(world.comments_on(321)) == 1
    assert world.discover() == []


def test_a_refusal_whose_issue_changed_before_its_write_is_not_written_as_planned() -> None:
    """Review r2 F4: the same reason for another issue is another refusal."""
    world = _World()
    world.config.filtering.label = None
    world.issue(231, "agent:backend")
    world.issue(232, "agent:backend")  # neither is in the effort
    pr = world.pr(321, issue=231)
    planned = _refusal(world)
    pr.body, pr.branch = "Closes #232", "hand-made-321"

    assert _skipped(world.apply(planned))
    assert world.comments_on(321) == []
    fresh = _refusal(world)
    assert (fresh.refusal, fresh.issue_number) == (planned.refusal, 232)
    world.apply(fresh)
    [comment] = world.comments_on(321)
    assert "issue #232" in comment and "issue #231" not in comment


def test_a_report_about_one_issue_never_stands_for_another() -> None:
    world = _World()
    world.config.filtering.label = None
    world.issue(231, "agent:backend")
    world.issue(232, "agent:backend")
    pr = world.pr(321, issue=231)
    world.apply(_refusal(world))
    pr.body, pr.branch = "Closes #232", "hand-made-321"

    world.apply(_refusal(world))

    first, second = world.comments_on(321)
    assert "issue #231" in first and "issue #232" in second
    assert world.discover() == []


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
