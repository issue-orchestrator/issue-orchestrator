from __future__ import annotations

from pathlib import Path
from typing import Any

from issue_orchestrator.domain.models import ORCHESTRATOR_PR_MARKER
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.pull_request_tracker import PRInfo

from .scenario_dsl import scenario, script


TRIGGER_LABEL = "retrospective-review"
REVIEWED_LABEL = "retrospective-reviewed"
CHANGES_REQUESTED_LABEL = "retrospective-changes-requested"


def _enable_retrospective_review(config) -> None:
    config.retrospective_review_enabled = True
    config.retrospective_review_trigger_label = TRIGGER_LABEL
    config.retrospective_reviewed_label = REVIEWED_LABEL
    config.retrospective_changes_requested_label = CHANGES_REQUESTED_LABEL
    config.session_grace_period_seconds = 0
    config.session_log_activity_seconds = 0


def _seed_prior_orchestrator_pr(issue_number: int, pr_number: int):
    def _mutate(repo_host) -> None:
        repo_host.prs[f"{issue_number}-completed"] = [
            PRInfo(
                number=pr_number,
                title=f"Fix #{issue_number}",
                url=f"https://github.com/test/repo/pull/{pr_number}",
                branch=f"{issue_number}-completed",
                body=f"{ORCHESTRATOR_PR_MARKER}\n\nPrior implementation.",
                state="merged",
                labels=[],
            )
        ]

    return _mutate


def _assert_issue_state(ctx, state: str) -> None:
    issue = ctx.repo_host.get_issue(ctx.issue_number)
    assert issue is not None
    assert issue.state == state


def _assert_label_removed(ctx, label: str) -> None:
    assert ctx.repo_host.remove_label_calls.count((ctx.issue_number, label)) == 1


def _assert_issue_model_lacks_label(ctx, label: str) -> None:
    issue = ctx.repo_host.get_issue(ctx.issue_number)
    assert issue is not None
    assert label not in issue.labels


def _event_payloads(ctx, name: EventName) -> list[dict[str, Any]]:
    return [
        event.data
        for event in ctx.events_since_baseline()
        if event.name == name
    ]


def _review_started_payload(ctx, task: str) -> dict[str, Any]:
    matches = [
        payload
        for payload in _event_payloads(ctx, EventName.REVIEW_STARTED)
        if payload.get("task") == task
    ]
    assert len(matches) == 1, f"expected exactly one {task} review start"
    return matches[0]


def _assert_run_scoped_fields(payload: dict[str, Any]) -> None:
    for key in (
        "run_id",
        "run_dir",
        "completion_path",
        "completion_path_absolute",
        "session_prompt_path",
    ):
        assert isinstance(payload.get(key), str) and payload[key], key


def _assert_retrospective_review_started_event(
    ctx,
    *,
    issue_number: int,
    prior_pr_number: int | None,
    prior_pr_url: str | None,
    source_agent: str,
) -> None:
    payload = _review_started_payload(ctx, "retrospective-review")
    assert payload["issue_number"] == issue_number
    assert payload["prior_pr_number"] == prior_pr_number
    assert payload["prior_pr_url"] == prior_pr_url
    assert payload["agent"] == "agent:reviewer"
    assert payload["source_agent"] == source_agent
    assert payload["task"] == "retrospective-review"
    assert payload["session_name"] == f"retrospective-review-{issue_number}"
    assert payload["trigger_label"] == TRIGGER_LABEL
    _assert_run_scoped_fields(payload)


def _assert_review_rework_review_order(ctx, *, issue_number: int, prior_pr_number: int) -> None:
    observed: list[tuple[str, dict[str, Any]]] = []
    for event in ctx.events_since_baseline():
        if event.name == EventName.REVIEW_STARTED:
            task = event.data.get("task")
            if task in {"retrospective-review", "review"}:
                observed.append((task, event.data))
        elif event.name == EventName.REWORK_STARTED:
            observed.append(("rework", event.data))

    assert [name for name, _ in observed] == [
        "retrospective-review",
        "rework",
        "review",
    ]

    retrospective = observed[0][1]
    assert retrospective["issue_number"] == issue_number
    assert retrospective["prior_pr_number"] == prior_pr_number

    rework = observed[1][1]
    assert rework["issue_number"] == issue_number
    assert rework["pr_number"] == prior_pr_number
    assert rework["agent"] == "agent:coder"
    assert rework["task"] == "rework"
    assert rework["rework_cycle"] == 1
    _assert_run_scoped_fields(rework)

    review = observed[2][1]
    assert review["issue_number"] == issue_number
    assert review["pr_number"] == 100
    assert review["agent"] == "agent:reviewer"
    assert review["task"] == "review"
    assert review["session_name"] == "review-100"
    _assert_run_scoped_fields(review)


def _assert_created_pr_contract(ctx) -> None:
    pr = ctx.repo_host.get_pr(100)
    assert pr is not None
    assert pr.number == 100
    assert pr.url == "https://github.com/test/repo/pull/100"
    assert pr.state == "open"
    assert ORCHESTRATOR_PR_MARKER in pr.body
    assert "code-reviewed" in pr.labels
    assert "needs-code-review" not in pr.labels


def test_retrospective_review_approval_marks_closed_issue_reviewed(
    scenario_repo: Path,
) -> None:
    ctx = (
        scenario("retrospective_review_approval", scenario_repo)
        .issue(
            number=365,
            title="Closed issue needing retrospective review",
            labels=["simulated-scenario", "agent:coder", TRIGGER_LABEL],
            state="closed",
        )
        .coder(script("coder_complete.sh"))
        .reviewer(script("reviewer_approved.sh"))
        .review_exchange(mode="via-draft-pr")
        .configure(_enable_retrospective_review)
        .configure_repo_host(_seed_prior_orchestrator_pr(365, 2365))
        .expect_latest_event(
            EventName.REVIEW_STARTED,
            predicate=lambda data: (
                data.get("task") == "retrospective-review"
                and data.get("prior_pr_number") == 2365
            ),
        )
        .expect_session_prompt_contains(
            "RETROSPECTIVE REVIEW MODE",
            session_name_prefix="retrospective-review",
        )
        .expect_session_prompt_contains(
            "Prior orchestrator PR: #2365",
            session_name_prefix="retrospective-review",
        )
        .expect_issue_label(REVIEWED_LABEL)
        .run()
    )

    _assert_issue_state(ctx, "closed")
    _assert_label_removed(ctx, TRIGGER_LABEL)
    _assert_issue_model_lacks_label(ctx, TRIGGER_LABEL)
    labels = ctx.repo_host.get_issue_labels(ctx.issue_number)
    assert REVIEWED_LABEL in labels
    assert CHANGES_REQUESTED_LABEL not in labels
    _assert_retrospective_review_started_event(
        ctx,
        issue_number=365,
        prior_pr_number=2365,
        prior_pr_url="https://github.com/test/repo/pull/2365",
        source_agent="agent:coder",
    )
    assert ctx.repo_host.get_pr(100) is None
    assert ctx.orch.state.pending_reworks == []


def test_retrospective_review_changes_requested_runs_coder_rework_then_pr_review(
    scenario_repo: Path,
) -> None:
    ctx = (
        scenario("retrospective_review_changes_to_rework", scenario_repo)
        .issue(
            number=366,
            title="Closed issue with stale implementation",
            labels=["simulated-scenario", "agent:coder", TRIGGER_LABEL],
            state="closed",
        )
        .coder(script("coder_complete.sh"))
        .reviewer(script("reviewer_retrospective_changes_then_approve.sh"))
        .review_exchange(mode="via-draft-pr")
        .configure(_enable_retrospective_review)
        .configure_repo_host(_seed_prior_orchestrator_pr(366, 2366))
        .expect_session_prompt_contains(
            "Prior orchestrator PR: #2366",
            session_name_prefix="retrospective-review",
        )
        .wait_for(
            lambda orch: (
                len(orch.state.session_history) >= 3
                and not orch.state.active_sessions
                and not orch.state.pending_reworks
                and not orch.state.pending_reviews
            ),
            max_ticks=10,
        )
        .expect_issue_label(CHANGES_REQUESTED_LABEL)
        .expect_pr(created=True)
        .expect_pr_label("code-reviewed")
        .expect_pr_lacks_label("needs-code-review")
        .run()
    )

    _assert_issue_state(ctx, "open")
    _assert_label_removed(ctx, TRIGGER_LABEL)
    _assert_issue_model_lacks_label(ctx, TRIGGER_LABEL)
    labels = ctx.repo_host.get_issue_labels(ctx.issue_number)
    assert CHANGES_REQUESTED_LABEL in labels
    assert REVIEWED_LABEL not in labels
    assert "needs-rework" not in labels
    _assert_retrospective_review_started_event(
        ctx,
        issue_number=366,
        prior_pr_number=2366,
        prior_pr_url="https://github.com/test/repo/pull/2366",
        source_agent="agent:coder",
    )
    _assert_review_rework_review_order(ctx, issue_number=366, prior_pr_number=2366)
    _assert_created_pr_contract(ctx)
    assert any(entry.pr_url for entry in ctx.orch.state.session_history)


def test_retrospective_review_changes_requested_without_coder_escalates_to_human(
    scenario_repo: Path,
) -> None:
    ctx = (
        scenario("retrospective_review_no_coder", scenario_repo)
        .issue(
            number=367,
            title="Closed issue with reviewer-only label",
            labels=["simulated-scenario", "agent:reviewer", TRIGGER_LABEL],
            state="closed",
        )
        .coder(script("coder_complete.sh"))
        .reviewer(script("reviewer_changes_requested.sh"))
        .review_exchange(mode="via-draft-pr")
        .configure(_enable_retrospective_review)
        .expect_session_prompt_contains(
            "No prior orchestrator PR with the expected signature was found.",
            session_name_prefix="retrospective-review",
        )
        .wait_for(
            lambda orch: "needs-human"
            in orch.deps.repository_host.get_issue_labels(367),
            max_ticks=6,
        )
        .expect_issue_label(CHANGES_REQUESTED_LABEL)
        .expect_issue_label("needs-human")
        .expect_issue_comment_contains("Retrospective Review Needs Human")
        .run()
    )

    _assert_issue_state(ctx, "open")
    _assert_label_removed(ctx, TRIGGER_LABEL)
    _assert_issue_model_lacks_label(ctx, TRIGGER_LABEL)
    labels = ctx.repo_host.get_issue_labels(ctx.issue_number)
    assert CHANGES_REQUESTED_LABEL in labels
    assert REVIEWED_LABEL not in labels
    assert "needs-rework" not in labels
    _assert_retrospective_review_started_event(
        ctx,
        issue_number=367,
        prior_pr_number=None,
        prior_pr_url=None,
        source_agent="agent:reviewer",
    )
    assert ctx.repo_host.get_pr(100) is None
    assert ctx.orch.state.pending_reworks == []
