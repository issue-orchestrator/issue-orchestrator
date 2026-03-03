from __future__ import annotations

import asyncio
from pathlib import Path

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.pull_request_tracker import PRInfo

from .conftest import build_config, build_orchestrator, run_until
from .scenario_dsl import scenario, script


def _issue(number: int) -> Issue:
    return Issue(
        number=number,
        title=f"Synthetic recovery issue #{number}",
        labels=["simulated-scenario", "agent:coder"],
    )


def _pr(*, number: int, issue_number: int, branch: str, labels: list[str]) -> PRInfo:
    return PRInfo(
        number=number,
        title=f"PR #{number} for issue #{issue_number}",
        url=f"https://github.com/test/repo/pull/{number}",
        branch=branch,
        body=f"Closes #{issue_number}\n\n<!-- orchestrator-managed-pr -->",
        state="open",
        labels=labels,
        draft=True,
    )


def _review_only_config(repo_root: Path):
    config = build_config(
        repo_root,
        coder_command=script("coder_complete.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        review_exchange_mode="via-draft-pr",
    )
    config.max_concurrent_sessions = 0
    return config


def test_startup_review_recovery_is_idempotent_without_duplicate_pending_reviews(scenario_repo: Path) -> None:
    config = _review_only_config(scenario_repo)
    issue = _issue(1)
    review_label = config.code_review_label or "needs-code-review"
    pr = _pr(number=801, issue_number=1, branch="1-sim", labels=[review_label])

    orch, repo_host, _events, _timeline = build_orchestrator(scenario_repo, [issue], config)
    repo_host.prs["1-sim"] = [pr]

    asyncio.run(orch.startup())
    assert [r.pr_number for r in orch.state.pending_reviews] == [801]

    asyncio.run(orch.startup())
    assert [r.pr_number for r in orch.state.pending_reviews] == [801]


def test_restart_recovers_changes_requested_state_from_labels(scenario_repo: Path) -> None:
    ctx = scenario("restart_recover_changes_requested", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_requested.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "needs-rework" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=14,
        ) \
        .run()

    restarted = ctx.restart()
    run_until(
        restarted.orch,
        lambda: (
            bool(restarted.orch.state.pending_reworks)
            or any(e.name == EventName.REWORK_STARTED for e in restarted.events_since_baseline())
        ),
        max_ticks=12,
    )
    assert (
        bool(restarted.orch.state.pending_reworks)
        or any(e.name == EventName.REWORK_STARTED for e in restarted.events_since_baseline())
    )


def test_restart_changes_requested_flow_can_recover_to_approved(scenario_repo: Path) -> None:
    ctx = scenario("restart_recover_to_approved", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_requested.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "needs-rework" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=14,
        ) \
        .run()

    # Simulate a restart where reviewer behavior has improved.
    ctx.config.agents["agent:reviewer"].command = script("reviewer_approved.sh")
    restarted = ctx.restart()
    run_until(
        restarted.orch,
        lambda: (
            restarted.repo_host.get_pr(100) is not None
            and "code-reviewed" in restarted.repo_host.get_pr(100).labels
        ),
        max_ticks=24,
    )

    pr = restarted.repo_host.get_pr(100)
    assert pr is not None
    assert "code-reviewed" in pr.labels
    assert "needs-rework" not in pr.labels


def test_restart_recovery_emits_rework_or_review_progress_events(scenario_repo: Path) -> None:
    ctx = scenario("restart_recovery_events", scenario_repo) \
        .coder(script("coder_complete.sh")) \
        .reviewer(script("reviewer_changes_requested.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .wait_for(
            lambda orch: (
                orch.deps.repository_host.get_pr(100) is not None
                and "needs-rework" in orch.deps.repository_host.get_pr(100).labels
            ),
            max_ticks=14,
        ) \
        .run()

    restarted = ctx.restart()
    run_until(
        restarted.orch,
        lambda: any(
            e.name in {EventName.REWORK_STARTED, EventName.REVIEW_STARTED, EventName.REVIEW_CHANGES_REQUESTED}
            for e in restarted.events_since_baseline()
        ),
        max_ticks=14,
    )

    event_names = [e.name for e in restarted.events_since_baseline()]
    assert any(
        name in {EventName.REWORK_STARTED, EventName.REVIEW_STARTED, EventName.REVIEW_CHANGES_REQUESTED}
        for name in event_names
    )


def test_restart_recovery_does_not_duplicate_pending_reviews_for_same_pr(scenario_repo: Path) -> None:
    config = _review_only_config(scenario_repo)
    issue = _issue(1)
    review_label = config.code_review_label or "needs-code-review"
    pr = _pr(number=802, issue_number=1, branch="1-sim", labels=[review_label])

    orch1, repo_host, _events1, _timeline1 = build_orchestrator(scenario_repo, [issue], config)
    repo_host.prs["1-sim"] = [pr]
    asyncio.run(orch1.startup())
    assert [r.pr_number for r in orch1.state.pending_reviews] == [802]
    orch1.state.pending_reviews.clear()  # simulate memory loss

    orch2, _repo_host2, _events2, _timeline2 = build_orchestrator(
        scenario_repo, [issue], config, repo_host=repo_host
    )
    asyncio.run(orch2.startup())
    for _ in range(6):
        orch2.tick()

    recovered = [r.pr_number for r in orch2.state.pending_reviews]
    assert recovered == [802]
