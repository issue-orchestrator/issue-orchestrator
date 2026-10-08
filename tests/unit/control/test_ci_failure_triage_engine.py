"""A failed required check, end to end through the engine (#8692).

Discovery (the real awaiting-merge reconciler, via ``GitHubWorkflow``) ->
CI-failure triage -> the real planner -> the re-run write. The repository host
is a mock at the port boundary; everything above it is production code.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.ci_failure_triage import apply_rerun_failed_checks
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.models import Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import (
    FailedCheck,
    FailedChecksRead,
    PRInfo,
    StatusCheckRollupRead,
)

HEAD = "c0ffee" + "0" * 34
CHECK = "workspace watch · windows process tree"
RUNNER_LOST = (
    "2026-10-08T06:31:40.0000000Z Running tests...\n"
    "2026-10-08T06:31:44.0000000Z ##[error]The hosted runner: GitHub Actions 7 "
    "lost communication with the server.\n"
)
TEST_FAILED = (
    "2026-10-08T06:01:02.0000000Z test watch::process_tree ... FAILED\n"
    "2026-10-08T06:01:02.1000000Z E       AssertionError: expected 3 children, saw 2\n"
    "2026-10-08T06:01:03.0000000Z ##[error]Process completed with exit code 1.\n"
)


class _Engine:
    """One repo's engine around a mock GitHub that remembers comments."""

    def __init__(self) -> None:
        self.config = Config(repo="owner/repo")
        self.comments: list[tuple[int, str]] = []
        self.logs: dict[int, str] = {}
        host = MagicMock()
        pr = PRInfo(
            number=318, title="Watch the process tree", url="https://github.com/owner/repo/pull/318",
            branch="228-process-tree", body="", state="open", labels=["code-reviewed"],
            mergeable_state="unstable", head_sha=HEAD,
        )
        host.get_pr.return_value = pr
        host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(state="FAILURE")
        host.get_issue.return_value = Issue(
            number=228, title="Process tree", labels=["agent:backend", "pr-pending"], state="open",
        )
        host.issue_comment_marker_present.return_value = False
        host.add_comment.side_effect = lambda number, body: self.comments.append((number, body))
        host.issue_comment_bodies_containing.side_effect = lambda number, needle: tuple(
            body for n, body in self.comments if n == number and needle in body
        )
        host.read_check_job_log_tail.side_effect = lambda job_id, max_bytes: self.logs[job_id][-max_bytes:]
        self.host = host
        self.state = OrchestratorState(session_history=[SessionHistoryEntry(
            issue_number=228, title="Process tree", agent_type="agent:backend", status="completed",
            runtime_minutes=0, pr_url="https://github.com/owner/repo/pull/318",
        )])
        self.workflow = GitHubWorkflow(
            config=self.config, events=InMemoryEventSink(), repository_host=host,
            fact_gatherer=MagicMock(), pr_scanner=MagicMock(), label_sync=None,
            event_context=EventContext(), label_manager=LabelManager(self.config),
        )

    def fail(self, job_id: int, log: str) -> None:
        """The PR's one required check failed as Actions job ``job_id``."""
        self.logs[job_id] = log
        self.host.read_failed_checks.return_value = FailedChecksRead(
            head_sha=HEAD, checks=(FailedCheck(CHECK, "FAILURE", True, job_id, 900),),
        )

    def tick(self):
        """Discover and plan, as one engine tick does; the facts are consumed."""
        self.state.awaiting_merge_rollup_scan_timestamps.clear()  # due every tick here
        self.workflow.scan_awaiting_merge_followups(self.state)
        snapshot = OrchestratorSnapshot(
            issues=(), active_sessions=(), pending_reviews=(), pending_reworks=(),
            pending_tech_lead=(), paused=False,
            discovered_reworks=tuple(self.state.discovered_reworks),
            discovered_ci_reruns=tuple(self.state.discovered_ci_reruns),
        )
        reworks, reruns = list(self.state.discovered_reworks), list(self.state.discovered_ci_reruns)
        self.state.discovered_reworks.clear()
        self.state.discovered_ci_reruns.clear()
        plan = Planner(config=self.config, scheduler=Scheduler(self.config)).plan(snapshot)
        return reworks, reruns, plan


def test_transient_failure_is_rerun_once_without_spending_a_rework_cycle() -> None:
    engine = _Engine()
    engine.fail(11, RUNNER_LOST)

    reworks, reruns, plan = engine.tick()
    assert reworks == []
    assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []
    assert plan.actions_of_type(ActionType.ADD_LABEL) == []  # no needs-rework, no cycle label
    (rerun,) = plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert (rerun.pr_number, rerun.head_sha, rerun.run_ids) == (318, HEAD, (900,))
    assert apply_rerun_failed_checks(
        rerun, rerun=engine.host.rerun_failed_check_jobs, post_comment=engine.host.add_comment,
    ).success
    engine.host.rerun_failed_check_jobs.assert_called_once_with(900)
    (_, record), = engine.comments
    assert "lost communication with the server" in record  # the signature is recorded

    # GitHub has not restarted the job yet: the same failure is the re-run starting.
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS) == []

    # The re-run (job 12) fails transiently again on the same head: no second
    # re-run - it goes to rework, and that rework is the FIRST cycle.
    engine.fail(12, RUNNER_LOST)
    reworks, reruns, plan = engine.tick()
    assert reruns == []
    (rework,) = reworks
    assert rework.rework_cycle == 1
    assert "re-runs already spent on this head: 1" in (rework.feedback or "")
    assert len(plan.actions_of_type(ActionType.QUEUE_REWORK)) == 1
    engine.host.rerun_failed_check_jobs.assert_called_once_with(900)


def test_genuine_failure_log_reaches_the_rework_brief() -> None:
    engine = _Engine()
    engine.fail(21, TEST_FAILED)

    reworks, reruns, plan = engine.tick()

    assert reruns == []
    (queued,) = plan.actions_of_type(ActionType.QUEUE_REWORK)
    brief = queued.feedback or ""
    assert "Classification: genuine" in brief
    assert "AssertionError: expected 3 children, saw 2" in brief
    assert "2026-10-08T06:01" not in brief  # timestamps stripped from the excerpt
    engine.host.rerun_failed_check_jobs.assert_not_called()
    # A genuine failure never reads the re-run record: no extra API call.
    engine.host.issue_comment_bodies_containing.assert_not_called()


def test_a_jobs_log_is_read_once() -> None:
    engine = _Engine()
    engine.fail(21, TEST_FAILED)
    engine.tick()
    engine.tick()
    assert engine.host.read_check_job_log_tail.call_count == 1


def test_non_required_failure_is_not_read_when_a_required_one_failed() -> None:
    engine = _Engine()
    engine.fail(21, TEST_FAILED)
    engine.logs[31] = RUNNER_LOST
    read = engine.host.read_failed_checks.return_value
    engine.host.read_failed_checks.return_value = replace(
        read, checks=(*read.checks, FailedCheck("lint (optional)", "FAILURE", False, 31, 901)),
    )
    engine.tick()
    assert [c.args[0] for c in engine.host.read_check_job_log_tail.call_args_list] == [21]



def test_a_rerun_retried_after_a_failed_write_spends_one_liveness_budget() -> None:
    """The record's request time is not a fact: retries share one budget (#7350)."""
    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    retried = replace(rerun, comment=rerun.comment.replace("at=", "at=2099"))
    assert retried.comment != rerun.comment
    assert retried.liveness_facts() == rerun.liveness_facts()


def test_a_refused_rerun_records_nothing_and_never_becomes_a_rework() -> None:
    """A credential without Actions write: no record, the re-run is planned again
    (for the liveness owner to bound and escalate), and no rework cycle is spent."""
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.host.rerun_failed_check_jobs.side_effect = RepositoryHostError("403 Resource not accessible by integration")
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    result = apply_rerun_failed_checks(
        rerun, rerun=engine.host.rerun_failed_check_jobs, post_comment=engine.host.add_comment,
    )
    assert not result.success
    assert engine.comments == []

    reworks, _, plan = engine.tick()
    assert reworks == [] and plan.actions_of_type(ActionType.QUEUE_REWORK) == []
    (again,) = plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert again.liveness_facts() == rerun.liveness_facts()  # one liveness budget
