"""Tests for the queued-rework status owner (#6588)."""

from __future__ import annotations

from issue_orchestrator.control.awaiting_merge_post_publish_policy import (
    POST_PUBLISH_VALIDATION_SOURCE,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    DiscoveredRework,
    OrchestratorState,
    PendingRework,
)
from issue_orchestrator.view_models.rework_status import (
    format_queued_rework_summary,
    queued_rework_issue_numbers,
    resolve_queued_rework,
)


def test_resolve_none_when_not_queued() -> None:
    assert resolve_queued_rework(OrchestratorState(), 454) is None


def test_resolve_from_pending_rework_post_publish_conflict() -> None:
    state = OrchestratorState(
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("454"),
                agent_type="agent:web",
                rework_cycle=2,
                issue_number=454,
                pr_number=469,
                source=POST_PUBLISH_VALIDATION_SOURCE,
                feedback=(
                    "Merge conflict against base branch (cycle handled by "
                    "post-publish gate, not the reviewer):\n\ndetails"
                ),
            )
        ]
    )
    status = resolve_queued_rework(state, 454)
    assert status is not None
    assert status.pr_number == 469
    assert status.rework_cycle == 2
    # The parenthetical qualifier and trailing colon are trimmed to a phrase.
    assert status.reason == "Merge conflict against base branch"
    assert status.summary == (
        "Queued for rework — PR #469 (cycle 2): Merge conflict against base branch"
    )


def test_resolve_from_discovered_rework_review_label() -> None:
    state = OrchestratorState(
        discovered_reworks=[
            DiscoveredRework(
                issue_number=77,
                pr_number=88,
                branch_name="fix",
                agent_type="agent:web",
                rework_cycle=1,
                source="review_label",
            )
        ]
    )
    status = resolve_queued_rework(state, 77)
    assert status is not None
    assert status.reason == "Reviewer requested changes"
    assert "PR #88" in status.summary


def test_pending_rework_wins_over_discovered() -> None:
    state = OrchestratorState(
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("5"),
                agent_type="agent:web",
                rework_cycle=3,
                issue_number=5,
                pr_number=50,
                source="review_label",
            ),
        ],
        discovered_reworks=[
            DiscoveredRework(
                issue_number=5,
                pr_number=50,
                branch_name="b",
                agent_type="agent:web",
                rework_cycle=1,
                source="review_label",
            ),
        ],
    )
    status = resolve_queued_rework(state, 5)
    assert status is not None
    # The authoritative queued cycle wins over the raw scan.
    assert status.rework_cycle == 3


def test_discovered_review_label_without_pr_number_reads_as_no_pr() -> None:
    state = OrchestratorState(
        discovered_reworks=[
            DiscoveredRework(
                issue_number=9,
                pr_number=0,
                branch_name="b",
                agent_type="agent:web",
                rework_cycle=1,
                source="review_label",
            ),
        ],
    )
    status = resolve_queued_rework(state, 9)
    assert status is not None
    assert status.pr_number is None
    assert status.summary == "Queued for rework (cycle 1): Reviewer requested changes"


def test_queued_rework_issue_numbers_unions_pending_and_discovered() -> None:
    state = OrchestratorState(
        pending_reworks=[
            PendingRework(
                issue_key=FakeIssueKey("5"),
                agent_type="agent:web",
                rework_cycle=1,
                issue_number=5,
                pr_number=50,
                source="review_label",
            ),
        ],
        discovered_reworks=[
            DiscoveredRework(
                issue_number=77,
                pr_number=88,
                branch_name="fix",
                agent_type="agent:web",
                rework_cycle=1,
                source="review_label",
            ),
        ],
    )
    # The lane-eligibility set agrees with resolve_queued_rework: every number
    # it reports resolves to a status, and only those numbers do.
    assert queued_rework_issue_numbers(state) == frozenset({5, 77})
    assert resolve_queued_rework(state, 5) is not None
    assert resolve_queued_rework(state, 77) is not None
    assert resolve_queued_rework(state, 999) is None


def test_queued_rework_issue_numbers_empty_without_reworks() -> None:
    assert queued_rework_issue_numbers(OrchestratorState()) == frozenset()


def test_format_helper_matches_status_summary() -> None:
    assert format_queued_rework_summary(
        469, 1, "Merge conflict against base branch"
    ) == "Queued for rework — PR #469 (cycle 1): Merge conflict against base branch"
    # A zero/unknown cycle renders as cycle 1.
    assert format_queued_rework_summary(None, 0, "Rework requested") == (
        "Queued for rework (cycle 1): Rework requested"
    )
