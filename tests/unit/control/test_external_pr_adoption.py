"""Externally authored PRs in an integration effort (#9499), at the port boundaries.

The repository host is the in-memory fake (``MockGitHubAdapter``); the
standing-rulings owner is the real one over faked issue bodies. Discovery
(the real ``GitHubWorkflow``), the planner's step actions, the applier's
re-checks and writes, review discovery (the real ``PRScanner``) and the
integration owner's merge gate are production code.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.awaiting_merge_reconciler import AwaitingMergeReconciler
from issue_orchestrator.control.board_snapshot_builder import BoardSnapshotBuilder
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.integration_branch import (
    IntegrationBranchOwner,
    apply_integration_step,
    plan_integration_steps,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.pr_scanner import PRScanner
from issue_orchestrator.domain.board_snapshot import BoardTechLeadWriteHealth
from issue_orchestrator.domain.external_pr_adoption import (
    AdoptExternalPullRequest,
    AdoptionRefusal,
    ReportRefusedExternalPullRequest,
    refusal_marker,
)
from issue_orchestrator.domain.integration_branch import BranchComparison, MergeIntoIntegration
from issue_orchestrator.domain.models import (
    ORCHESTRATOR_PR_MARKER,
    Issue,
    OrchestratorState,
    PendingReview,
    PendingRework,
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

    def pr(
        self, number: int, *, issue: int | None, base: str = "integration", body: str | None = None,
        labels: tuple[str, ...] = (), draft: bool = False, branch: str | None = None,
    ) -> PRInfo:
        pr = PRInfo(
            number=number, title=f"Hand-made {number}", url=f"https://github.com/owner/repo/pull/{number}",
            branch=branch or (f"{issue}-hand-made" if issue is not None else f"hand-made-{number}"),
            body=body if body is not None else (f"Closes #{issue}" if issue is not None else "A change"),
            state="open", labels=list(labels), base_branch=base, head_sha=HEAD, draft=draft,
            mergeable_state="clean", status_check_rollup="SUCCESS",
        )
        self.host.prs.setdefault(pr.branch, []).append(pr)
        self.host.labels[number] = set(labels)
        return pr

    def discover(self, *, issue_branches: dict[int, str] | None = None) -> list:
        self.state.discovered_integration_steps.clear()
        self.workflow.adopt_external_prs(self.state, issue_branches=issue_branches or {})
        return list(self.state.discovered_integration_steps)

    def apply(self, step):
        [action] = plan_integration_steps([step])
        return apply_integration_step(action, host=self.host, labels=self.labels, rulings=self.rulings,
                                      events=self.events)

    def adopt_round(self, **kwargs) -> list:
        steps = self.discover(**kwargs)
        for step in steps:
            self.apply(step)
        return steps

    def comments_on(self, number: int) -> list[str]:
        return [comment["body"] for comment in self.host.comments if comment["number"] == number]


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
    scan = PRScanner(world.config, world.host, InMemoryEventSink(), label_manager=world.labels).scan_for_reviews(
        [], [], issue_branches={},
    )
    assert [(review.issue_number, review.pr_number) for review in scan.reviews] == [(231, 321)]
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
        world.pr(321, issue=231, body=f"Closes #231\n\n*{ORCHESTRATOR_PR_MARKER}*")
    elif kind == "draft":
        world.pr(321, issue=231, draft=True)
    else:
        world.pr(321, issue=None, base="main", branch="integration", body="Deliver integration to main")

    assert world.discover() == []
    assert world.host.add_label_calls == [] and world.host.comments == []


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

    assert world.discover() == []
    assert world.state.external_pull_requests.reported == {}


def test_a_refusal_whose_reason_changes_is_told_again() -> None:
    world = _World()
    world.pr(321, issue=None)
    world.apply(_refusal(world))
    world.effort_issue(231)
    world.host.prs["hand-made-321"][0].body = "Closes #231"  # now it references an issue ...
    world.host.prs["hand-made-321"][0].base_branch = "main"  # ... from the wrong base

    step = _refusal(world)
    world.apply(step)

    assert step.refusal is AdoptionRefusal.WRONG_BASE
    assert len(world.comments_on(321)) == 2


@pytest.mark.parametrize("where", ["branch", "history"])
def test_an_issue_io_is_already_working_is_not_adopted_twice(where: str) -> None:
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231)
    branches = {231: "231-io-attempt"} if where == "branch" else {}
    if where == "history":
        world.state.session_history.append(SessionHistoryEntry(
            issue_number=231, title="Issue 231", agent_type="agent:backend", status="completed",
            runtime_minutes=3, pr_url="https://github.com/owner/repo/pull/300",
        ))

    [step] = world.discover(issue_branches=branches)

    assert isinstance(step, ReportRefusedExternalPullRequest)
    assert step.refusal is AdoptionRefusal.IO_WORK_IN_FLIGHT


def test_an_unreadable_listing_adopts_and_refuses_nothing() -> None:
    world = _World()
    world.effort_issue(231)
    world.pr(321, issue=231)
    world.state.external_pull_requests.reported[400] = AdoptionRefusal.NO_ISSUE_REFERENCE

    def refuse(**_kwargs):
        raise RepositoryHostError("502 listing pulls")

    world.host.list_prs = refuse  # type: ignore[method-assign]

    assert world.discover() == []
    assert world.state.external_pull_requests.reported == {400: AdoptionRefusal.NO_ISSUE_REFERENCE}


def test_without_integration_mode_nothing_is_listed() -> None:
    world = _World()
    world.config.integration = IntegrationConfig(enabled=False)
    world.host.list_prs = MagicMock(side_effect=AssertionError("no listing"))  # type: ignore[method-assign]

    assert world.discover() == []


# -- the write re-checks ---------------------------------------------------------


@pytest.mark.parametrize("change", ["retargeted", "closed", "left_effort"])
def test_an_adoption_re_judged_at_the_write_is_skipped_when_the_pr_moved(change: str) -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231)
    [step] = world.discover()
    if change == "retargeted":
        pr.base_branch = "main"
    elif change == "closed":
        pr.state = "closed"
    else:
        world.host.labels[231].discard(EFFORT)

    result = world.apply(step)

    assert result.result_type is ActionResultType.SKIPPED  # type: ignore[attr-defined]
    assert pr.labels == []


# -- fail closed at the merge ------------------------------------------------------


def _approved(world: _World, pr: PRInfo, issue_number: int) -> None:
    """What io's reviewer approving the PR leaves behind: the label and the issue's history."""
    pr.labels.append(world.labels.code_reviewed)
    world.host.labels[pr.number].add(world.labels.code_reviewed)
    world.host.comparisons[(TIP, HEAD)] = BranchComparison(ahead_by=1, behind_by=0)
    world.state.session_history.append(SessionHistoryEntry(
        issue_number=issue_number, title=f"Issue {issue_number}", agent_type="agent:backend",
        status="completed", runtime_minutes=0, pr_url=pr.url,
    ))


def _merge_pass(world: _World):
    owner = IntegrationBranchOwner(
        config=world.config.integration, host=world.host, label_manager=world.labels,
        standing_rulings=world.rulings, clock=lambda: 1_000_000.0,
    )
    result = AwaitingMergeReconciler(
        world.host, label_manager=world.labels, repo="owner/repo", integration=owner,
    ).discover(world.state)
    return owner.discovered_steps(), result


def test_an_adopted_approved_pr_is_merged_like_io_s_own() -> None:
    world = _World()
    world.effort_issue(231)
    pr = world.pr(321, issue=231)
    world.adopt_round()
    _approved(world, pr, 231)

    steps, _ = _merge_pass(world)

    [merge] = steps
    assert isinstance(merge, MergeIntoIntegration) and merge.pr_number == 321
    assert world.apply(merge).result_type is ActionResultType.SUCCESS  # type: ignore[attr-defined]
    [merged] = world.host.pr_merges
    assert (merged["branch"], merged["head_sha"]) == ("integration", HEAD)
    assert pr.state == "merged"


@pytest.mark.parametrize("state", ["not_adopted", "left_effort"])
def test_io_never_merges_reworks_or_updates_an_unadmitted_external_pr(state: str) -> None:
    """A review label applied by hand cannot carry an unadopted PR into the branch."""
    world = _World()
    world.effort_issue(231)
    labels = ("origin:external",) if state == "left_effort" else ()  # adopted once, then the issue left
    pr = world.pr(321, issue=231, labels=labels)
    if state == "left_effort":
        world.host.get_issue(231).labels.remove(EFFORT)  # type: ignore[union-attr]
        world.host.labels[231].discard(EFFORT)
    _approved(world, pr, 231)
    pr.mergeable_state = "dirty"  # a conflict would otherwise send it to an io rework

    steps, result = _merge_pass(world)

    assert steps == [] and result.reworks == () and result.escalations == ()
    # The write refuses it too, on fresh reads.
    write = world.apply(MergeIntoIntegration(
        issue_number=231, issue_key="owner/repo#231", pr_number=321, pr_url=pr.url, pr_title=pr.title,
        head_sha=HEAD, integration_branch="integration", integration_tip=TIP, gate_label="code-reviewed",
    ))
    assert write.result_type is ActionResultType.SKIPPED  # type: ignore[attr-defined]
    assert state in write.details["skip_reason"]  # type: ignore[attr-defined]
    assert world.host.pr_merges == []


def test_an_adopted_pr_whose_issue_carries_a_ruling_is_handed_to_a_person() -> None:
    world = _World()
    world.effort_issue(231)
    world.bodies.bodies[231] = body_with(a_ruling())
    pr = world.pr(321, issue=231)
    world.adopt_round()
    _approved(world, pr, 231)

    steps, result = _merge_pass(world)

    assert steps == []
    [escalation] = result.escalations
    assert (escalation.kind, escalation.pr_number) == ("integration_ruling_check", 321)


# -- the board ---------------------------------------------------------------------


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

    builder = BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: [], log_tail_provider=lambda n: [],
        case_file_reader=lambda: (), shipped_fix_reader=lambda limit: (), e2e_health_reader=lambda now: None,
        tech_lead_write_health_reader=lambda now: BoardTechLeadWriteHealth(
            verdict="idle", is_alarm=False, reason="test", stale_after_hours=48.0,
        ),
        session_activity_reader=lambda session: None, clock=lambda: datetime(2026, 10, 13, 12, 0),
    )

    queues = builder.build(world.state).queues

    details = [entry.detail for entry in queues]
    assert details[0].endswith("[origin:external]")
    assert "[origin:external]" not in details[1]
    assert "PR #321 [origin:external]" in details[2]
