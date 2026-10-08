"""Integration-branch mode end to end through the engine (#8144).

The #8144 acceptance, in process: two approved PRs and a sibling that the
first merges leave behind. Discovery (the real ``GitHubWorkflow`` and
awaiting-merge reconciler), the real planner and the integration step writes
run tick by tick against a small simulated GitHub - a commit graph, branch
heads, PRs that merge and update the way GitHub does. Both PRs land in the
integration branch with no operator action, the sibling is brought up to the
tip mechanically with no rework cycle spent, every merged PR's issue is closed
by the reconciler's close-on-merge path, and the delivery PR lists all three.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import MagicMock

from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.integration_branch import apply_integration_step
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.integration_branch import (
    BranchComparison,
    BranchMergeOutcome,
    MergedIntoBranch,
    delivered_tip,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import IntegrationConfig
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.repository_host import RepositoryHostError
from tests.conftest import MockGitHubAdapter
from tests.standing_ruling_helpers import IssueBodies, a_ruling, body_with, rulings_owner


class _GitHub(MockGitHubAdapter):
    """GitHub as integration mode sees it: a commit graph, heads, merges and updates."""

    def __init__(self) -> None:
        super().__init__(repo="owner/repo")
        self._shas = count(1)
        self.ancestry: dict[str, frozenset[str]] = {}
        self.main = self.commit()
        self.branches = {"main": self.main}
        #: PRs whose branch truly conflicts with the integration tip.
        self.conflicting: set[int] = set()

    def commit(self, *parents: str) -> str:
        sha = f"{next(self._shas):040x}"
        self.ancestry[sha] = frozenset({sha}).union(*(self.ancestry[p] for p in parents))
        return sha

    def _resolve(self, ref: str) -> str:
        return self.branches.get(ref, ref)

    def compare_commits(self, base: str, head: str) -> BranchComparison:
        self.compare_calls.append((base, head))
        base_commits, head_commits = self.ancestry[self._resolve(base)], self.ancestry[self._resolve(head)]
        ahead = head_commits - base_commits
        return BranchComparison(ahead_by=len(ahead), behind_by=len(base_commits - head_commits),
                                commit_shas=tuple(sorted(ahead)))

    def merge_pull_request(self, pr_number: int, *, head_sha: str, method: str, title: str, message: str) -> str:
        pr = self.get_pr(pr_number)
        assert pr is not None and pr.head_sha == head_sha and pr.state == "open"
        base = pr.base_branch
        assert base is not None
        merged = self.commit(self.branches[base], head_sha)
        self.branches[base] = merged
        pr.state, pr.merged_at = "merged", "2026-10-08T12:00:00Z"
        self.merged_into[base] = (*self.merged_into.get(base, ()), MergedIntoBranch(
            number=pr.number, title=pr.title, url=pr.url, merge_commit_sha=merged,
        ))
        self.pr_merges.append({"pr_number": pr_number, "head_sha": head_sha, "method": method, "title": title})
        return merged

    def update_pull_request_branch(self, pr_number: int, *, expected_head_sha: str) -> None:
        pr = self.get_pr(pr_number)
        assert pr is not None and pr.head_sha == expected_head_sha and pr.base_branch is not None
        if pr_number in self.conflicting:
            raise RepositoryHostError("merge conflict between base and head")
        self.pr_branch_updates.append((pr_number, expected_head_sha))
        pr.head_sha = self.commit(expected_head_sha, self.branches[pr.base_branch])
        pr.mergeable_state = "clean"  # its checks run green on the new head

    def merge_branch(self, *, base: str, head: str, message: str) -> BranchMergeOutcome:
        self.branch_merges.append((base, head, message))
        self.branches[base] = self.commit(self.branches[base], self._resolve(head))
        return BranchMergeOutcome.MERGED


class _Engine:
    def __init__(self) -> None:
        self.config = Config(repo="owner/repo")
        self.config.integration = IntegrationConfig(enabled=True)
        self.labels = LabelManager(self.config)
        self.github = _GitHub()
        self.bodies = IssueBodies()
        self.now = 1_000_000.0
        self.workflow = GitHubWorkflow(
            config=self.config, events=InMemoryEventSink(), repository_host=self.github,
            fact_gatherer=MagicMock(), pr_scanner=MagicMock(), label_sync=None,
            event_context=EventContext(), label_manager=self.labels,
            standing_rulings=rulings_owner(self.bodies), clock=lambda: self.now,
        )
        self.state = OrchestratorState()
        self.events = InMemoryEventSink()
        self.reworks: list = []
        self.escalations: list = []
        self.closed: set[int] = set()

    def approved_pr(self, issue_number: int, pr_number: int) -> PRInfo:
        """A reviewer-approved PR into integration, branched from its current tip."""
        self.github.issues.append(Issue(
            number=issue_number, title=f"Issue {issue_number}", labels=["agent:backend", "pr-pending"], state="open",
        ))
        pr = PRInfo(
            number=pr_number, title=f"Fix {issue_number}", url=f"https://github.com/owner/repo/pull/{pr_number}",
            branch=f"{issue_number}-fix", body=f"Fixes #{issue_number}", state="open",
            labels=[self.labels.code_reviewed], mergeable_state="clean", base_branch="integration",
            head_sha=self.github.commit(self.github.branches["integration"]),
        )
        self.github.prs.setdefault(pr.branch, []).append(pr)
        self.state.session_history.append(SessionHistoryEntry(
            issue_number=issue_number, title=f"Issue {issue_number}", agent_type="agent:backend",
            status="completed", runtime_minutes=0, pr_url=pr.url,
        ))
        return pr

    def tick(self) -> None:
        """One engine tick: discover, plan, apply every integration action."""
        self.now += 61
        self.state.awaiting_merge_rollup_scan_timestamps.clear()
        self.workflow.scan_awaiting_merge_followups(self.state)
        snapshot = OrchestratorSnapshot(
            issues=(), active_sessions=(), pending_reviews=(), pending_reworks=(), pending_tech_lead=(),
            paused=False,
            discovered_reworks=tuple(self.state.discovered_reworks),
            discovered_awaiting_merge_escalations=tuple(self.state.discovered_awaiting_merge_escalations),
            discovered_awaiting_merge_reconciliations=tuple(self.state.discovered_awaiting_merge_reconciliations),
            discovered_integration_steps=tuple(self.state.discovered_integration_steps),
        )
        self.reworks.extend(self.state.discovered_reworks)
        self.escalations.extend(self.state.discovered_awaiting_merge_escalations)
        for fact in self.state.discovered_awaiting_merge_reconciliations:
            if fact.issue_open:
                self.closed.add(fact.issue_number)  # the close-on-merge fallback's subject
        for buffer in (
            self.state.discovered_reworks, self.state.discovered_awaiting_merge_escalations,
            self.state.discovered_awaiting_merge_reconciliations, self.state.discovered_integration_steps,
        ):
            buffer.clear()
        plan = Planner(config=self.config, scheduler=Scheduler(self.config)).plan(snapshot)
        for action in plan.actions_of_type(ActionType.ADVANCE_INTEGRATION):
            apply_integration_step(action, host=self.github, labels=self.labels, events=self.events)
        # A merged PR leaves the awaiting-merge history once reconciled.
        self.state.session_history = [
            entry for entry in self.state.session_history if entry.issue_number not in self.closed
        ]


def test_two_approved_prs_and_a_left_behind_sibling_all_land_without_a_rework_cycle() -> None:
    engine = _Engine()
    engine.tick()  # creates the integration branch from main
    assert engine.github.branches["integration"] == engine.github.main

    first, second, sibling = engine.approved_pr(228, 318), engine.approved_pr(229, 319), engine.approved_pr(230, 320)
    for _ in range(8):
        engine.tick()

    assert [merge["pr_number"] for merge in engine.github.pr_merges] == [318, 319, 320]
    assert {first.state, second.state, sibling.state} == {"merged"}
    # The merges left the others behind; each was brought up mechanically.
    assert {pr for pr, _ in engine.github.pr_branch_updates} == {319, 320}
    assert engine.reworks == [] and engine.escalations == []
    # Each PR merged at a head that contained the tip it merged into.
    integration = engine.github.branches["integration"]
    assert all(
        engine.github.compare_commits(integration, pr.head_sha or "").ahead_by == 0  # merged: nothing ahead
        for pr in (first, second, sibling)
    )
    # GitHub closes no issue on a non-default-branch merge: io's reconcile does.
    assert engine.closed == {228, 229, 230}

    view = engine.state.integration_delivery
    assert view is not None and view.merged_pr_numbers == (318, 319, 320)
    delivery = engine.github.open_pr_refs[("integration", "main")]
    assert delivered_tip(delivery.body) == integration
    assert all(f"#{n} Fix {n - 90}" in delivery.body for n in (318, 319, 320))


def test_delivery_then_integration_continues_from_main() -> None:
    engine = _Engine()
    engine.tick()
    engine.approved_pr(228, 318)
    for _ in range(3):
        engine.tick()
    assert engine.state.integration_delivery is not None

    # The operator merges the delivery PR with a merge commit.
    github = engine.github
    github.branches["main"] = github.commit(github.branches["main"], github.branches["integration"])
    engine.tick()

    assert github.branches["integration"] == github.branches["main"]
    assert engine.state.integration_delivery is None


def test_a_true_conflict_goes_to_rework_and_a_ruling_to_a_person() -> None:
    engine = _Engine()
    engine.tick()
    engine.approved_pr(228, 318)
    sibling = engine.approved_pr(229, 319)
    engine.bodies.bodies[230] = body_with(a_ruling())
    ruled = engine.approved_pr(230, 320)
    engine.github.conflicting.add(319)

    for _ in range(4):
        engine.tick()
        if sibling.mergeable_state != "dirty" and engine.github.pr_merges:
            sibling.mergeable_state = "dirty"  # GitHub now reports the conflict

    assert [merge["pr_number"] for merge in engine.github.pr_merges] == [318]
    assert [rework.pr_number for rework in engine.reworks][:1] == [319]
    assert [(e.pr_number, e.kind) for e in engine.escalations][:1] == [(320, "integration_ruling_check")]
    assert ruled.state == "open"
