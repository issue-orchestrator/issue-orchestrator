from __future__ import annotations

import pytest

import asyncio
import shutil
from pathlib import Path

from issue_orchestrator.domain.models import Issue, ORCHESTRATOR_PR_MARKER
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.pull_request_tracker import PRInfo

from .conftest import StubWorkingCopy, build_config, build_orchestrator, run_until
from .scenario_dsl import scenario, script


class NoRemoteBranchWorkingCopy(StubWorkingCopy):
    """WorkingCopy stub that simulates no remote branches for startup analysis."""

    def list_remote_branches(self, repo_root: Path, remote: str = "origin") -> list[str]:
        return []


def _issue(*labels: str) -> Issue:
    return Issue(
        number=1,
        title="Synthetic restart recovery issue",
        labels=list(labels),
    )


def _config(repo_root: Path):
    return build_config(
        repo_root,
        coder_command=script("coder_complete.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        review_exchange_mode="via-draft-pr",
    )


def test_startup_recovers_open_pr_label_state_after_restart(scenario_repo: Path) -> None:
    """Startup should recover stale in-progress state when an open PR exists."""
    config = _config(scenario_repo)
    issue = _issue("simulated-scenario", "agent:coder", "in-progress")
    pr = PRInfo(
        number=321,
        title="PR for #1",
        url="https://github.com/test/repo/pull/321",
        branch="1-sim",
        body=f"Closes #1\n\n{ORCHESTRATOR_PR_MARKER}",
        state="open",
        labels=[],
        draft=False,
    )

    orch, repo_host, _events, _timeline = build_orchestrator(
        scenario_repo,
        [issue],
        config,
        working_copy=StubWorkingCopy(branch="1-sim"),
    )
    repo_host.prs["1-sim"] = [pr]

    asyncio.run(orch.startup())

    assert (1, "pr-pending") in repo_host.add_label_calls
    assert (1, "in-progress") in repo_host.remove_label_calls


def test_startup_clears_orphaned_in_progress_label_without_branch_or_session(scenario_repo: Path) -> None:
    """Startup should clear stale in-progress label when no session/branch/PR exists."""
    config = _config(scenario_repo)
    issue = _issue("simulated-scenario", "agent:coder", "in-progress")

    orch, repo_host, _events, _timeline = build_orchestrator(
        scenario_repo,
        [issue],
        config,
        working_copy=NoRemoteBranchWorkingCopy(),
    )

    asyncio.run(orch.startup())

    assert (1, "in-progress") in repo_host.remove_label_calls


def test_restart_recovers_pending_review_queue_from_pr_labels(scenario_repo: Path) -> None:
    """Restart should rebuild pending_reviews from PR labels, not old in-memory state."""
    config = _config(scenario_repo)
    issue = _issue("simulated-scenario", "agent:coder")
    pr = PRInfo(
        number=444,
        title="Needs review",
        url="https://github.com/test/repo/pull/444",
        branch="1-sim",
        body=f"Closes #1\n\n{ORCHESTRATOR_PR_MARKER}",
        state="open",
        labels=[config.code_review_label],
        draft=True,
    )

    orch1, repo_host, _events1, _timeline1 = build_orchestrator(
        scenario_repo,
        [issue],
        config,
        working_copy=StubWorkingCopy(branch="1-sim"),
    )
    repo_host.prs["1-sim"] = [pr]
    asyncio.run(orch1.startup())
    assert len(orch1.state.pending_reviews) == 1
    # Simulate in-memory loss before restart; recovery must come from PR labels.
    orch1.state.pending_reviews.clear()

    orch2, _repo_host2, _events2, _timeline2 = build_orchestrator(
        scenario_repo,
        [issue],
        config,
        repo_host=repo_host,
        working_copy=StubWorkingCopy(branch="1-sim"),
    )
    asyncio.run(orch2.startup())

    assert len(orch2.state.pending_reviews) == 1
    assert orch2.state.pending_reviews[0].pr_number == 444
    assert orch2.state.pending_reviews[0].issue_number == 1


@pytest.mark.skip(
    reason="Persistent-session cutover deleted the spawn-per-phase "
    "capture path these scenarios were tightly coupled to. The "
    "persistent runner is exhaustively unit-tested in "
    "test_persistent_session_exchange.py + test_persistent_round_runner.py; "
    "migrating this harness to drive the persistent runner natively "
    "is tracked as a follow-up."
)
def test_validation_retry_state_recovers_after_restart(scenario_repo: Path) -> None:
    """Restart should recover persisted validation retry state and relaunch it."""
    ctx = scenario("retry_recovery_restart", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=1) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .wait_for_event(EventName.SESSION_VALIDATION_RETRY_NEEDED) \
        .run()
    assert len(ctx.orch.state.pending_validation_retries) == 0
    assert any(
        session.issue.number == ctx.issue_number and session.validation_retry_count == 1
        for session in ctx.orch.state.active_sessions
    )
    expected_recovery_worktree = ctx.config.worktree_base / f"{ctx.config.repo_root.name}-{ctx.issue_number}"
    assert ctx.worktree is not None
    shutil.copytree(ctx.worktree, expected_recovery_worktree, dirs_exist_ok=True)

    restarted_orch, _repo_host, _events, _timeline = build_orchestrator(
        ctx.repo_root,
        list(ctx.repo_host.issues),
        ctx.config,
        repo_host=ctx.repo_host,
        working_copy=StubWorkingCopy(branch="1-sim"),
    )
    asyncio.run(restarted_orch.startup())
    assert len(restarted_orch.state.pending_validation_retries) == 1
    assert restarted_orch.state.pending_validation_retries[0].issue_number == ctx.issue_number

    run_until(
        restarted_orch,
        lambda: any(
            session.issue.number == ctx.issue_number and session.validation_retry_count == 1
            for session in restarted_orch.state.active_sessions
        ),
        max_ticks=6,
    )
    assert len(restarted_orch.state.pending_validation_retries) == 0


def test_tick_boundaries_and_session_lifecycle_remain_consistent(scenario_repo: Path) -> None:
    """Synthetic run should maintain tick boundary invariants around session lifecycle."""
    ctx = scenario("tick_lifecycle_contract", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for_event(EventName.SESSION_COMPLETED) \
        .run()

    event_names = [event.name for event in ctx.events_since_baseline()]
    tick_started = [i for i, name in enumerate(event_names) if name == EventName.TICK_STARTED]
    tick_completed = [i for i, name in enumerate(event_names) if name == EventName.TICK_COMPLETED]

    assert tick_started, "expected at least one tick.started event"
    assert len(tick_started) == len(tick_completed), "tick start/completed count mismatch"
    for start_idx, completed_idx in zip(tick_started, tick_completed):
        assert start_idx < completed_idx, "tick.completed must occur after tick.started"

    assert EventName.SESSION_STARTED in event_names
    assert EventName.SESSION_COMPLETED in event_names
    assert event_names.index(EventName.SESSION_STARTED) < event_names.index(EventName.SESSION_COMPLETED)
