"""A failed required check, end to end through the engine (#8692).

Discovery (the real awaiting-merge reconciler, via ``GitHubWorkflow``) ->
CI-failure triage -> the real planner -> the re-run write. The repository host
is a mock at the port boundary; everything above it is production code.
"""

from __future__ import annotations

import re
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
    CheckRunAttempt,
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
        # GitHub's runs: each run's current attempt and that attempt's job ids.
        # Accepting a re-run starts a new attempt whose jobs have not run yet.
        self.runs: dict[int, CheckRunAttempt] = {}
        self.refused_runs: set[int] = set()
        self.attempts_visible = True  # GitHub may show an accepted re-run's attempt late
        host.rerun_failed_check_jobs.side_effect = self._rerun
        host.read_check_run_latest_attempt.side_effect = lambda run_id: self.runs[run_id]
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

    def _rerun(self, run_id: int) -> None:
        from issue_orchestrator.ports.repository_host import RepositoryHostError

        if run_id in self.refused_runs:
            raise RepositoryHostError("403 Resource not accessible by integration")
        if self.attempts_visible:
            self.runs[run_id] = CheckRunAttempt(attempt=self.runs[run_id].attempt + 1, job_ids=frozenset())

    def apply(self, action) -> bool:
        """The applier's write, with this engine's GitHub."""
        return apply_rerun_failed_checks(
            action, rerun=self.host.rerun_failed_check_jobs, post_comment=self.host.add_comment,
        ).success

    def age_records(self) -> None:
        """io's re-run records were written longer ago than the start grace."""
        self.comments = [
            (n, re.sub(r"at=\S+ -->", "at=2026-01-01T00:00:00+00:00 -->", body)) for n, body in self.comments
        ]

    def fail(self, job_id: int, log: str, *, attempt: int = 1) -> None:
        """The PR's one required check failed as Actions job ``job_id`` of run 900's ``attempt``."""
        self.fail_jobs({job_id: log}, attempt=attempt, names={job_id: CHECK})

    def fail_jobs(self, logs: dict[int, str], *, attempt: int = 1, names=None, runs=None) -> None:
        """These required jobs failed, each in its run's ``attempt``."""
        self.logs.update(logs)
        runs = runs or {job: 900 for job in logs}
        names = names or {job: f"shard {job}" for job in logs}
        self.host.read_failed_checks.return_value = FailedChecksRead(head_sha=HEAD, checks=tuple(
            FailedCheck(names[job], "FAILURE", True, job, runs[job]) for job in logs
        ))
        for run in set(runs.values()):
            self.runs[run] = CheckRunAttempt(
                attempt=attempt, job_ids=frozenset(job for job in logs if runs[job] == run),
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
            discovered_awaiting_merge_escalations=tuple(self.state.discovered_awaiting_merge_escalations),
        )
        reworks, reruns = list(self.state.discovered_reworks), list(self.state.discovered_ci_reruns)
        self.state.discovered_reworks.clear()
        self.state.discovered_ci_reruns.clear()
        self.state.discovered_awaiting_merge_escalations.clear()
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
    assert engine.apply(rerun)
    engine.host.rerun_failed_check_jobs.assert_called_once_with(900)
    (_, record), = engine.comments
    assert "lost communication with the server" in record  # the signature is recorded

    # GitHub has not restarted the job yet: the same failure is the re-run starting.
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS) == []

    # The re-run (job 12) fails transiently again on the same head: no second
    # re-run - it goes to rework, and that rework is the FIRST cycle.
    engine.fail(12, RUNNER_LOST, attempt=2)
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




def test_an_unreadable_failed_check_list_defers_the_rework_a_bounded_number_of_scans() -> None:
    from issue_orchestrator.control.ci_failure_triage import MAX_READ_DEFERRALS
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    engine = _Engine()
    engine.host.read_failed_checks.side_effect = RepositoryHostError("502 Bad Gateway")
    for _ in range(MAX_READ_DEFERRALS):
        reworks, reruns, plan = engine.tick()
        assert (reworks, reruns) == ([], [])
        assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []
    (rework,) = engine.tick()[0]
    assert "could not read the PR's failed checks: 502 Bad Gateway" in (rework.feedback or "")


def test_an_unreadable_rerun_record_never_spends_a_rework_cycle_at_once() -> None:
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.host.issue_comment_bodies_containing.side_effect = RepositoryHostError("503")
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []


def test_every_failed_required_job_is_read_before_a_rerun_is_decided() -> None:
    from issue_orchestrator.control.ci_failure_triage import MAX_LOG_READS_PER_SCAN

    engine = _Engine()
    jobs = {40 + n: RUNNER_LOST for n in range(MAX_LOG_READS_PER_SCAN + 2)}
    engine.fail_jobs(jobs)
    assert engine.tick()[:2] == ([], [])  # this scan's read budget ran out
    reworks, (rerun,), _ = engine.tick()
    assert reworks == [] and rerun.job_ids == tuple(sorted(jobs))
    assert sorted(c.args[0] for c in engine.host.read_check_job_log_tail.call_args_list) == sorted(jobs)


def test_a_genuine_failure_decides_without_reading_every_log() -> None:
    engine = _Engine()
    engine.fail_jobs({50: TEST_FAILED, **{51 + n: RUNNER_LOST for n in range(6)}})
    (rework,), reruns, _ = engine.tick()
    assert reruns == []
    assert "AssertionError: expected 3 children, saw 2" in (rework.feedback or "")
    assert "Logs not read" in (rework.feedback or "")
    assert [c.args[0] for c in engine.host.read_check_job_log_tail.call_args_list] == [50]



def test_a_new_head_without_a_failed_check_is_not_reworked() -> None:
    """The rollup failed on the old head; a push since leaves nothing failed."""
    engine = _Engine()
    engine.host.read_failed_checks.return_value = FailedChecksRead(head_sha="b" * 40, checks=())
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []


def test_an_unreadable_attempt_keeps_the_log_for_the_brief() -> None:
    from issue_orchestrator.control.ci_failure_triage import MAX_READ_DEFERRALS
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.host.read_check_run_latest_attempt.side_effect = RepositoryHostError("502 Bad Gateway")
    for _ in range(MAX_READ_DEFERRALS):
        assert engine.tick()[:2] == ([], [])
    (rework,), reruns, _ = engine.tick()
    assert reruns == []
    assert "lost communication with the server" in (rework.feedback or "")
    assert engine.host.read_check_job_log_tail.call_count == 1  # the log is not re-read



def test_a_failed_record_write_asks_github_nothing() -> None:
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.host.add_comment.side_effect = RepositoryHostError("502 Bad Gateway")
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert not engine.apply(rerun)
    engine.host.rerun_failed_check_jobs.assert_not_called()


def test_a_refused_rerun_is_never_asked_twice_and_goes_to_a_person() -> None:
    """A credential without Actions write: io's record (its intent) is on the PR
    and the request was refused. io never asks twice for a head: after the start
    grace a person is asked, and no coding rework is queued."""
    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.refused_runs.add(900)
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert not engine.apply(rerun)
    assert len(engine.comments) == 1

    assert engine.tick()[:2] == ([], [])  # within the grace: the request may be in flight
    engine.age_records()
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []
    (escalation,) = plan.actions_of_type(ActionType.ESCALATE_TO_HUMAN)
    assert "ci_rerun_unconfirmed" in escalation.escalation_reason
    engine.host.rerun_failed_check_jobs.assert_called_once_with(900)

    # Once the person holds it, the engine neither re-escalates nor reworks it.
    engine.workflow.fact_gatherer.human_gates = None
    pr = engine.host.get_pr.return_value
    engine.host.get_pr.return_value = replace(pr, labels=[*pr.labels, LabelManager(engine.config).needs_human])
    reworks, reruns, plan = engine.tick()
    assert (reworks, reruns) == ([], [])
    assert plan.actions_of_type(ActionType.ESCALATE_TO_HUMAN) == []
    assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []


def test_an_accepted_rerun_github_does_not_show_is_never_asked_again() -> None:
    """GitHub accepted the re-run but shows no new attempt, and the engine
    restarted: io's record (written first) means exactly one request."""
    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.attempts_visible = False
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert engine.apply(rerun)
    engine.state = OrchestratorState(session_history=engine.state.session_history)  # restart
    assert engine.tick()[:2] == ([], [])
    engine.age_records()
    _, reruns, plan = engine.tick()
    assert reruns == [] and plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS) == []
    engine.host.rerun_failed_check_jobs.assert_called_once_with(900)


def test_a_rerun_github_shows_is_waited_for_even_unrecorded() -> None:
    """No record on the PR (a person re-ran it), and the re-run's jobs have not
    failed yet: no request and no rework, while the old failure is still shown."""
    engine = _Engine()
    engine.fail(11, RUNNER_LOST)
    engine.runs[900] = CheckRunAttempt(attempt=2, job_ids=frozenset({31}))
    for _ in range(6):  # more scans than any deferral bound
        reworks, reruns, plan = engine.tick()
        assert (reworks, reruns) == ([], [])
        assert plan.actions_of_type(ActionType.QUEUE_REWORK) == []
    engine.host.rerun_failed_check_jobs.assert_not_called()


def test_a_partly_refused_rerun_is_never_asked_twice() -> None:
    engine = _Engine()
    engine.fail_jobs({11: RUNNER_LOST, 12: RUNNER_LOST}, runs={11: 900, 12: 901})
    engine.refused_runs.add(901)
    (rerun,) = engine.tick()[2].actions_of_type(ActionType.RERUN_FAILED_CHECKS)
    assert rerun.run_ids == (900, 901)
    assert not engine.apply(rerun)
    engine.age_records()
    _, reruns, plan = engine.tick()
    assert reruns == [] and plan.actions_of_type(ActionType.RERUN_FAILED_CHECKS) == []
    assert [c.args[0] for c in engine.host.rerun_failed_check_jobs.call_args_list] == [900, 901]
