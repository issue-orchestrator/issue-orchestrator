"""Integration-branch mode's owner (#8144), at its port boundaries.

The repository host is the in-memory fake (``MockGitHubAdapter``); the
standing-rulings owner is the real one over faked issue bodies. Everything
between - the reconciler's post-approval dispatch, the owner's routing and
branch upkeep, the planner's step actions and the applier's re-checks - is
production code.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.control.awaiting_merge_reconciler import AwaitingMergeReconciler
from issue_orchestrator.control.integration_branch import (
    INTEGRATION_UPKEEP_INTERVAL_SECONDS,
    IntegrationBranchOwner,
    IntegrationConfigError,
    apply_integration_step,
    plan_integration_steps,
)
from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.integration_branch import (
    BranchComparison,
    BranchMergeOutcome,
    CreateIntegrationBranch,
    FastForwardIntegration,
    MergedIntoBranch,
    MergeIntoIntegration,
    OpenDeliveryPullRequest,
    OpenPullRequestRef,
    RefreshDeliveryPullRequest,
    SyncIntegrationFromDefault,
    UpdatePullRequestBranch,
    delivered_tip,
    delivery_marker,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import IntegrationConfig
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.repository_host import RepositoryHostError
from tests.conftest import MockGitHubAdapter
from tests.standing_ruling_helpers import IssueBodies, a_ruling, body_with, rulings_owner

TIP = "1" * 40
MAIN = "2" * 40
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _sha(n: int) -> str:
    return f"{n:040x}"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _World:
    """One repo with an integration branch, approved PRs into it, and the owner's collaborators."""

    def __init__(self, *, merge_after: str = "code-reviewed") -> None:
        self.config = Config(repo="owner/repo")
        self.config.integration = IntegrationConfig(enabled=True, merge_after=merge_after)
        self.labels = LabelManager(self.config)
        self.host = MockGitHubAdapter(repo="owner/repo")
        self.host.branches = {"main": MAIN, "integration": TIP}
        self.bodies = IssueBodies()
        self.rulings = rulings_owner(self.bodies)
        self.clock = _Clock()
        self.state = OrchestratorState()
        self.events = InMemoryEventSink()

    def owner(self) -> IntegrationBranchOwner:
        return IntegrationBranchOwner(
            config=self.config.integration, host=self.host, label_manager=self.labels,
            standing_rulings=self.rulings, clock=self.clock,
        )

    def approved_pr(
        self, issue_number: int, pr_number: int, head: str, *, mergeable_state: str = "clean",
        current: bool = True, base: str = "integration", labels: list[str] | None = None,
        rollup: str | None = "SUCCESS",
    ) -> PRInfo:
        issue = Issue(number=issue_number, title=f"Issue {issue_number}",
                      labels=["agent:backend", "pr-pending"], state="open")
        self.host.issues.append(issue)
        pr = PRInfo(
            number=pr_number, title=f"Fix {issue_number}", url=f"https://github.com/owner/repo/pull/{pr_number}",
            branch=f"{issue_number}-fix", body=f"Closes #{issue_number}", state="open",
            labels=labels if labels is not None else [self.labels.code_reviewed],
            mergeable_state=mergeable_state, base_branch=base, head_sha=head,
            status_check_rollup=rollup,  # type: ignore[arg-type]
        )
        self.host.prs.setdefault(pr.branch, []).append(pr)
        self.host.comparisons[(TIP, head)] = BranchComparison(ahead_by=1, behind_by=0 if current else 2)
        self.state.session_history.append(SessionHistoryEntry(
            issue_number=issue_number, title=issue.title, agent_type="agent:backend", status="completed",
            runtime_minutes=0, pr_url=pr.url,
        ))
        return pr

    def discover(self, owner: IntegrationBranchOwner):
        return AwaitingMergeReconciler(
            self.host, label_manager=self.labels, repo="owner/repo", integration=owner,
        ).discover(self.state)


# -- routing one approved PR ----------------------------------------------------


def test_a_behind_approved_pr_is_updated_mechanically_and_never_reworked() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A, current=False)
    owner = world.owner()

    result = world.discover(owner)

    assert result.reworks == ()
    assert owner.discovered_steps() == [_update_step()]


def test_github_reporting_behind_is_an_update_not_a_rework() -> None:
    """Branch protection on integration makes GitHub say ``behind``: still no agent cycle."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A, mergeable_state="behind", current=False)
    owner = world.owner()

    result = world.discover(owner)

    assert result.reworks == ()
    assert [type(step) for step in owner.discovered_steps()] == [UpdatePullRequestBranch]


def test_without_integration_mode_a_behind_pr_spends_a_rework_cycle() -> None:
    """The control for the test above: the default dispatch reworks a behind PR."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A, mergeable_state="behind", current=False)

    result = AwaitingMergeReconciler(world.host, label_manager=world.labels, repo="owner/repo").discover(world.state)

    assert [rework.pr_number for rework in result.reworks] == [318]


def test_a_current_green_pr_is_merged_at_its_head_and_tip() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    owner = world.owner()

    world.discover(owner)

    assert owner.discovered_steps() == [MergeIntoIntegration(
        issue_number=228, issue_key=owner.discovered_steps()[0].issue_key, pr_number=318,  # type: ignore[union-attr]
        pr_url="https://github.com/owner/repo/pull/318", pr_title="Fix 228", head_sha=HEAD_A,
        integration_branch="integration", integration_tip=TIP, gate_label="code-reviewed",
    )]


def test_merges_are_serialized_one_per_pass_oldest_first() -> None:
    world = _World()
    world.approved_pr(229, 319, HEAD_B)
    world.approved_pr(228, 318, HEAD_A)  # seen first: the history lists the newest entry first
    owner = world.owner()

    world.discover(owner)

    [merge] = owner.discovered_steps()
    assert isinstance(merge, MergeIntoIntegration) and merge.pr_number == 318


def test_every_behind_pr_is_updated_in_the_same_pass() -> None:
    """The tip moved: each open approved PR is brought up, not only the first."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A, current=False)
    world.approved_pr(229, 319, HEAD_B, current=False)
    owner = world.owner()

    world.discover(owner)

    assert sorted(step.pr_number for step in owner.discovered_steps()) == [318, 319]  # type: ignore[union-attr]
    assert all(isinstance(step, UpdatePullRequestBranch) for step in owner.discovered_steps())


@pytest.mark.parametrize("where", ["issue", "pr"])
def test_a_person_s_hold_keeps_io_from_merging_or_updating(where: str) -> None:
    world = _World()
    pr = world.approved_pr(228, 318, HEAD_A)
    if where == "issue":
        world.host.issues[0].labels.append(world.labels.needs_human)
    else:
        pr.labels.append(world.labels.needs_human)
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert result.escalations == ()


def test_merge_after_tech_lead_review_waits_for_that_label() -> None:
    world = _World(merge_after="tech-lead-reviewed")
    pr = world.approved_pr(228, 318, HEAD_A)
    owner = world.owner()

    world.discover(owner)
    assert owner.discovered_steps() == []

    pr.labels.append(world.labels.tech_lead_reviewed)
    owner = world.owner()
    world.discover(owner)
    assert [type(step) for step in owner.discovered_steps()] == [MergeIntoIntegration]


def test_a_pr_whose_issue_carries_a_ruling_is_handed_to_a_person_never_merged() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    world.bodies.bodies[228] = body_with(a_ruling())
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    [escalation] = result.escalations
    assert escalation.kind == "integration_ruling_check"
    assert escalation.pr_number == 318
    assert "m-0123456789ab" in escalation.reason


def test_unreadable_rulings_merge_nothing() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    world.bodies.unreadable.add(228)
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert result.escalations == ()


def test_a_real_conflict_still_goes_to_agent_rework() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A, mergeable_state="dirty")
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert [rework.pr_number for rework in result.reworks] == [318]


def test_checks_running_on_a_current_head_wait_without_a_step() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A, mergeable_state="unstable", rollup="PENDING")
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert result.reworks == ()
    assert 228 in world.state.awaiting_merge_checks_pending_since  # the timeout machine runs


def test_a_pr_into_the_default_branch_is_not_the_owner_s() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A, base="main")
    owner = world.owner()

    world.discover(owner)

    assert owner.discovered_steps() == []
    assert world.host.compare_calls == []


def test_an_unreadable_comparison_acts_on_nothing() -> None:
    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    world.host.integration_failures["compare_commits"] = RepositoryHostError("502")
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert result.reworks == ()


def test_the_owner_refuses_to_exist_when_the_mode_is_off() -> None:
    world = _World()
    world.config.integration = IntegrationConfig()
    with pytest.raises(IntegrationConfigError):
        world.owner()


# -- the branch and the delivery PR ---------------------------------------------


def test_a_missing_integration_branch_is_created_from_the_default_branch() -> None:
    world = _World()
    del world.host.branches["integration"]
    owner = world.owner()

    owner.upkeep(world.state)

    assert owner.discovered_steps() == [CreateIntegrationBranch(branch="integration", from_sha=MAIN)]


def test_after_delivery_integration_fast_forwards_to_the_default_branch() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=0, behind_by=1)
    owner = world.owner()

    owner.upkeep(world.state)

    assert owner.discovered_steps() == [FastForwardIntegration(branch="integration", from_sha=TIP, to_sha=MAIN)]
    assert world.state.integration_delivery is None


def test_when_both_moved_on_the_default_branch_is_merged_in() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=3, behind_by=1, commit_shas=(HEAD_A,))
    owner = world.owner()

    owner.upkeep(world.state)

    assert owner.discovered_steps()[0] == SyncIntegrationFromDefault(
        branch="integration", default_branch="main", integration_tip=TIP, default_tip=MAIN,
    )
    assert isinstance(owner.discovered_steps()[1], OpenDeliveryPullRequest)


def test_the_delivery_pr_is_opened_listing_the_prs_it_delivers() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(
        ahead_by=4, behind_by=0, commit_shas=(_sha(1), _sha(2), _sha(3), _sha(4)),
    )
    world.host.merged_into["integration"] = (
        MergedIntoBranch(number=318, title="Fix 228", url="u318", merge_commit_sha=_sha(2)),
        MergedIntoBranch(number=319, title="Fix 229", url="u319", merge_commit_sha=_sha(4)),
        MergedIntoBranch(number=300, title="Already delivered", url="u300", merge_commit_sha=_sha(99)),
    )
    owner = world.owner()

    owner.upkeep(world.state)

    [step] = owner.discovered_steps()
    assert isinstance(step, OpenDeliveryPullRequest)
    assert (step.head, step.base, step.integration_tip) == ("integration", "main", TIP)
    assert "- #318 Fix 228" in step.body and "- #319 Fix 229" in step.body
    assert "#300" not in step.body
    assert delivered_tip(step.body) == TIP
    assert "merge commit" in step.body


def test_a_stale_delivery_body_is_refreshed_and_the_page_sees_the_pr() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=1, behind_by=0, commit_shas=(_sha(5),))
    world.host.open_pr_refs[("integration", "main")] = OpenPullRequestRef(
        number=400, url="https://github.com/owner/repo/pull/400", body=delivery_marker(HEAD_C),
    )
    world.host.merged_into["integration"] = (
        MergedIntoBranch(number=318, title="Fix 228", url="u318", merge_commit_sha=_sha(5)),
    )
    owner = world.owner()

    owner.upkeep(world.state)

    [step] = owner.discovered_steps()
    assert isinstance(step, RefreshDeliveryPullRequest) and step.pr_number == 400
    view = world.state.integration_delivery
    assert view is not None
    assert (view.pr_number, view.integration_tip, view.merged_pr_numbers, view.ahead_by) == (400, TIP, (318,), 1)


def test_a_current_delivery_pr_is_left_alone() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=1, behind_by=0, commit_shas=(_sha(5),))
    world.host.open_pr_refs[("integration", "main")] = OpenPullRequestRef(
        number=400, url="https://github.com/owner/repo/pull/400", body=delivery_marker(TIP),
    )
    owner = world.owner()
    owner.upkeep(world.state)  # after a restart: no view yet, the body is current
    assert owner.discovered_steps() == []
    assert world.state.integration_delivery is not None

    world.clock.now += INTEGRATION_UPKEEP_INTERVAL_SECONDS
    world.host.integration_failures["merged_pull_requests_into"] = RepositoryHostError("must not be read")
    owner = world.owner()
    owner.upkeep(world.state)
    assert owner.discovered_steps() == []


def test_nothing_to_deliver_clears_the_page() -> None:
    world = _World()
    world.state.integration_delivery = object()  # type: ignore[assignment]
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=0, behind_by=0)
    owner = world.owner()

    owner.upkeep(world.state)

    assert owner.discovered_steps() == []
    assert world.state.integration_delivery is None


def test_upkeep_runs_at_most_once_per_interval() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=0, behind_by=0)
    world.owner().upkeep(world.state)
    calls = len(world.host.compare_calls)

    world.owner().upkeep(world.state)
    assert len(world.host.compare_calls) == calls

    world.clock.now += INTEGRATION_UPKEEP_INTERVAL_SECONDS
    world.owner().upkeep(world.state)
    assert len(world.host.compare_calls) == calls + 1


def test_an_integration_branch_that_is_the_default_branch_fails_loudly() -> None:
    world = _World()
    world.host.default_branch = "integration"
    with pytest.raises(IntegrationConfigError, match="default branch"):
        world.owner().upkeep(world.state)


def test_an_upkeep_read_failure_is_contained() -> None:
    world = _World()
    world.host.integration_failures["compare_commits"] = RepositoryHostError("502")
    owner = world.owner()

    owner.upkeep(world.state)

    assert owner.discovered_steps() == []


# -- the writes ---------------------------------------------------------------------


def _apply(world: _World, step) -> object:
    [action] = plan_integration_steps([step])
    return apply_integration_step(action, host=world.host, labels=world.labels, rulings=world.rulings,
                                  events=world.events)


def _merge_step(**overrides) -> MergeIntoIntegration:
    step = MergeIntoIntegration(
        issue_number=228, issue_key="owner/repo#228", pr_number=318, pr_url="u", pr_title="Fix 228",
        head_sha=HEAD_A, integration_branch="integration", integration_tip=TIP, gate_label="code-reviewed",
    )
    return replace(step, **overrides)


def _update_step(**overrides) -> UpdatePullRequestBranch:
    step = UpdatePullRequestBranch(
        issue_number=228, pr_number=318, head_sha=HEAD_A, integration_tip=TIP,
        integration_branch="integration", gate_label="code-reviewed",
    )
    return replace(step, **overrides)


def _ready_world() -> _World:
    """PR #318 approved, green, current: exactly what discovery made a merge step of."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    world.host.labels[318] = {world.labels.code_reviewed}
    return world


def test_the_merge_runs_at_the_checked_head_with_a_merge_commit() -> None:
    world = _ready_world()

    result = _apply(world, _merge_step())

    assert result.success  # type: ignore[attr-defined]
    [merge] = world.host.pr_merges
    assert (merge["branch"], merge["tip_sha"], merge["head_sha"]) == ("integration", TIP, HEAD_A)
    assert merge["message"].startswith("Merge #318: Fix 228\n\n")
    assert [event.event_type for event in world.events.events] == [EventName.INTEGRATION_STEP_APPLIED]


def test_a_hold_put_on_after_discovery_stops_the_merge() -> None:
    world = _ready_world()
    world.host.labels[318].add(world.labels.needs_human)

    result = _apply(world, _merge_step())

    assert not result.success  # type: ignore[attr-defined]
    assert world.host.pr_merges == []
    assert [event.event_type for event in world.events.events] == [EventName.INTEGRATION_STEP_SKIPPED]


def test_a_tip_that_moved_after_discovery_stops_the_merge() -> None:
    world = _ready_world()
    world.host.branches["integration"] = HEAD_C

    result = _apply(world, _merge_step())

    assert not result.success  # type: ignore[attr-defined]
    assert world.host.pr_merges == []


def test_a_refused_merge_is_a_failure_the_liveness_owner_bounds() -> None:
    world = _ready_world()
    world.host.integration_failures["merge_head_onto"] = RepositoryHostError("422 Update is not a fast forward")

    result = _apply(world, _merge_step())

    assert result.result_type is ActionResultType.FAILURE  # type: ignore[attr-defined]


def test_the_update_is_guarded_by_the_expected_head() -> None:
    world = _ready_world()

    _apply(world, _update_step())

    assert world.host.pr_branch_updates == [(318, HEAD_A)]


@pytest.mark.parametrize("change", ["hold", "gate_label", "retarget", "head", "closed"])
def test_an_update_re_judges_the_pr_at_the_write(change: str) -> None:
    """Review r2 F2: what discovery saw may change before the update is written."""
    world = _ready_world()
    pr = world.host.get_pr(318)
    assert pr is not None
    if change == "hold":
        world.host.labels[228] = {world.labels.needs_human}
    elif change == "gate_label":
        world.host.labels[318] = set()
    elif change == "retarget":
        pr.base_branch = "main"
    elif change == "head":
        pr.head_sha = HEAD_C
    else:
        pr.state = "closed"

    result = _apply(world, _update_step())

    assert world.host.pr_branch_updates == []
    assert result.result_type is ActionResultType.SKIPPED  # type: ignore[attr-defined]


def test_an_update_is_written_whatever_the_tip_moved_to() -> None:
    """An update brings the PR to the CURRENT tip: a moved tip is no reason to skip it."""
    world = _ready_world()
    world.host.branches["integration"] = HEAD_C

    _apply(world, _update_step())

    assert world.host.pr_branch_updates == [(318, HEAD_A)]


def test_a_tip_moved_between_the_check_and_the_write_is_refused_atomically() -> None:
    """Review r2 F1: the branch moves only from the checked tip, so a merge that
    raced another writer fails instead of landing on an unchecked base."""
    world = _ready_world()
    real_branch_head = world.host.branch_head

    def tip_moves_after_the_check(branch: str) -> str | None:
        head = real_branch_head(branch)
        world.host.branches["integration"] = HEAD_C  # another writer, right after io's read
        return head

    world.host.branch_head = tip_moves_after_the_check  # type: ignore[method-assign]

    result = _apply(world, _merge_step())

    assert world.host.pr_merges == []
    assert result.result_type is ActionResultType.FAILURE  # type: ignore[attr-defined]
    assert world.host.branches["integration"] == HEAD_C


def test_a_fast_forward_only_moves_the_branch_it_compared() -> None:
    world = _World()
    world.host.branches["integration"] = HEAD_C

    result = _apply(world, FastForwardIntegration(branch="integration", from_sha=TIP, to_sha=MAIN))

    assert result.result_type is ActionResultType.FAILURE  # type: ignore[attr-defined]
    assert world.host.fast_forwards == []

    world.host.branches["integration"] = TIP
    _apply(world, FastForwardIntegration(branch="integration", from_sha=TIP, to_sha=MAIN))
    assert world.host.fast_forwards == [("integration", MAIN)]


def test_a_conflicting_sync_is_refused_and_reported() -> None:
    world = _World()
    world.host.branch_merge_outcomes[("integration", MAIN)] = BranchMergeOutcome.CONFLICT

    result = _apply(world, SyncIntegrationFromDefault(
        branch="integration", default_branch="main", integration_tip=TIP, default_tip=MAIN,
    ))

    assert result.result_type is ActionResultType.FAILURE  # type: ignore[attr-defined]
    assert "conflicts" in (result.error or "")  # type: ignore[attr-defined]
    assert [event.event_type for event in world.events.events] == [EventName.INTEGRATION_STEP_SKIPPED]


def test_the_delivery_pr_is_opened_and_refreshed_through_the_host() -> None:
    world = _World()

    _apply(world, OpenDeliveryPullRequest(head="integration", base="main", title="Deliver integration to main",
                                          body="b1", integration_tip=TIP))
    opened = world.host.open_pr_refs[("integration", "main")]
    _apply(world, RefreshDeliveryPullRequest(pr_number=opened.number, body="b2", integration_tip=TIP))

    assert world.host.pr_body_updates == [(opened.number, "b2")]


def test_each_step_becomes_one_action_naming_its_subject() -> None:
    update = _update_step()
    create = CreateIntegrationBranch(branch="integration", from_sha=MAIN)

    actions = plan_integration_steps([update, create])

    assert [(a.issue_number, a.pr_number, a.step) for a in actions] == [(228, 318, update), (0, 0, create)]
    assert actions[0].liveness_facts() != actions[1].liveness_facts()


# -- what may change between discovery and the write (review r1 F1-F3) ----------


@pytest.mark.parametrize(("rollup", "routed"), [("PENDING", "wait"), (None, "wait"), ("FAILURE", "rework")])
def test_a_clean_pr_merges_only_on_green_checks_for_its_head(rollup: str | None, routed: str) -> None:
    """GitHub says ``clean`` on a branch without required checks even while
    they run or fail: io reads the head's checks itself."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A, rollup=rollup)
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    if routed == "rework":
        assert [rework.pr_number for rework in result.reworks] == [318]
    else:
        assert result.reworks == ()
        assert 228 in world.state.awaiting_merge_checks_pending_since  # the timeout will escalate


def test_unreadable_checks_for_lack_of_permission_go_to_a_person() -> None:
    from issue_orchestrator.ports.pull_request_tracker import StatusCheckRollupRead

    world = _World()
    world.approved_pr(228, 318, HEAD_A)
    world.host.read_commit_check_rollup = lambda sha: StatusCheckRollupRead(  # type: ignore[method-assign]
        state=None, capability="permission_denied", primary_source_denied=True,
    )
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert [e.kind for e in result.escalations] == ["status_rollup_permission_denied"]


def test_checks_that_turned_red_after_discovery_stop_the_merge() -> None:
    world = _ready_world()
    world.host.get_pr(318).status_check_rollup = "FAILURE"  # type: ignore[union-attr]

    _apply(world, _merge_step())

    assert world.host.pr_merges == []


def test_a_pr_retargeted_after_discovery_is_not_merged() -> None:
    world = _ready_world()
    world.host.get_pr(318).base_branch = "main"  # type: ignore[union-attr]

    result = _apply(world, _merge_step())

    assert world.host.pr_merges == []
    assert "now targets 'main'" in result.details["skip_reason"]  # type: ignore[attr-defined]


def test_a_head_that_moved_after_discovery_is_not_merged() -> None:
    world = _ready_world()
    world.host.get_pr(318).head_sha = HEAD_C  # type: ignore[union-attr]

    _apply(world, _merge_step())

    assert world.host.pr_merges == []


def test_a_ruling_recorded_after_discovery_stops_the_merge() -> None:
    world = _ready_world()
    world.bodies.bodies[228] = body_with(a_ruling())

    result = _apply(world, _merge_step())

    assert world.host.pr_merges == []
    assert "ruled" in result.details["skip_reason"]  # type: ignore[attr-defined]


def test_a_gate_label_removed_after_discovery_stops_the_merge() -> None:
    world = _ready_world()
    world.host.labels[318] = set()

    _apply(world, _merge_step())

    assert world.host.pr_merges == []


def test_a_truncated_merged_listing_marks_the_delivery_body_incomplete() -> None:
    world = _World()
    world.host.comparisons[("main", TIP)] = BranchComparison(ahead_by=1, behind_by=0, commit_shas=(_sha(5),))
    world.host.merged_listing_truncated.add("integration")
    owner = world.owner()

    owner.upkeep(world.state)

    [step] = owner.discovered_steps()
    assert isinstance(step, OpenDeliveryPullRequest) and "may be incomplete" in step.body


# -- no launch before the integration branch exists (review r2 F4) ---------------


def test_launches_wait_until_the_upkeep_has_seen_the_branch() -> None:
    from issue_orchestrator.control.integration_branch import integration_branch_missing

    world = _World()
    del world.host.branches["integration"]
    assert integration_branch_missing(world.config, world.state) == "integration"

    world.owner().upkeep(world.state)  # plans the create; the branch is not there yet
    assert integration_branch_missing(world.config, world.state) == "integration"

    world.host.branches["integration"] = MAIN  # the create was applied
    world.host.comparisons[("main", MAIN)] = BranchComparison(ahead_by=0, behind_by=0)
    world.clock.now += INTEGRATION_UPKEEP_INTERVAL_SECONDS
    world.owner().upkeep(world.state)
    assert integration_branch_missing(world.config, world.state) is None


def test_a_failed_create_keeps_every_launch_waiting() -> None:
    """The planner launches nothing while the branch is missing, so a refused
    create cannot strand worktrees fetching a branch that is not there."""
    from unittest.mock import MagicMock

    from issue_orchestrator.control.host_rate_limit_launch_gate import plan_launches_or_wait
    from issue_orchestrator.control.planner_types import OrchestratorSnapshot
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import PendingRework

    snapshot = OrchestratorSnapshot(
        issues=(), active_sessions=(), pending_reviews=(), pending_tech_lead=(), paused=False,
        pending_reworks=(PendingRework(issue_number=228, pr_number=318, agent_type="agent:backend",
                                       issue_key=FakeIssueKey(name="228")),),
        integration_branch_missing="integration",
    )
    plan_launches = MagicMock(side_effect=AssertionError("must not plan a launch"))

    actions, skipped = plan_launches_or_wait(
        snapshot, launch_log=MagicMock(), plan_launches=plan_launches, withdrawals=lambda items: [],
    )

    assert actions == []
    assert [(item.item_type, item.number) for item in skipped] == [("rework", 228)]
    assert "integration" in skipped[0].reason


def test_with_the_mode_off_nothing_waits_for_a_branch() -> None:
    from issue_orchestrator.control.integration_branch import integration_branch_missing

    world = _World()
    world.config.integration = IntegrationConfig()
    assert integration_branch_missing(world.config, world.state) is None


def test_the_checks_judged_are_those_of_the_exact_head_io_merges() -> None:
    """Review r3 F1: a check read by PR number could answer for a head pushed
    after io's read; io reads the checks of the head it will merge."""
    world = _ready_world()
    read: list[str] = []
    real = world.host.read_commit_check_rollup

    def spy(sha: str):
        read.append(sha)
        return real(sha)

    world.host.read_commit_check_rollup = spy  # type: ignore[method-assign]

    _apply(world, _merge_step())

    assert read == [HEAD_A]
    assert len(world.host.pr_merges) == 1


@pytest.mark.parametrize("where", ["issue", "pr"])
def test_rework_asked_for_on_either_item_blocks_the_merge(where: str) -> None:
    """Review r4 F2: needs-rework disqualifies, at discovery and at the write."""
    world = _World()
    pr = world.approved_pr(228, 318, HEAD_A)
    (world.host.issues[0].labels if where == "issue" else pr.labels).append(world.labels.needs_rework)
    owner = world.owner()
    world.discover(owner)
    assert owner.discovered_steps() == []

    world = _ready_world()
    world.host.labels.setdefault(228 if where == "issue" else 318, set()).add(world.labels.needs_rework)
    result = _apply(world, _merge_step())
    assert world.host.pr_merges == []
    assert "rework_requested" in result.details["skip_reason"]  # type: ignore[attr-defined]


def test_a_rulings_read_the_host_refuses_merges_nothing_and_keeps_the_scan() -> None:
    """Review r4 F3: the rulings owner's fresh issue read can fail with a host
    error; that fails closed instead of aborting the awaiting-merge scan."""
    world = _World()
    world.approved_pr(228, 318, HEAD_A)

    def refuse(number: int):
        raise RepositoryHostError("502 reading the issue body")

    world.rulings.read_issue = refuse  # type: ignore[method-assign]
    owner = world.owner()

    result = world.discover(owner)

    assert owner.discovered_steps() == []
    assert result.escalations == () and result.reworks == ()
