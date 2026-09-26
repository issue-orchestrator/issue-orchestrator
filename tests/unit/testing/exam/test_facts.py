"""Exam facts: GitHub call classes, PR states, and where an item stalled."""

from __future__ import annotations

import pytest

from issue_orchestrator.testing.exam import (
    EndpointClass,
    GitHubCallCounts,
    PullRequestState,
    classify_command,
)
from issue_orchestrator.testing.exam.stall import ItemEvent, stall_facts, unanswered_screen

from .builders import item, pr


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("GET /search/issues", EndpointClass.SEARCH),
        ("GET /search/code", EndpointClass.SEARCH),
        ("POST /graphql", EndpointClass.GRAPHQL),
        ("GET /repos/o/r/pulls", EndpointClass.CORE),
        ("PATCH /repos/o/r/issues/5", EndpointClass.CORE),
        ("GET /user", EndpointClass.CORE),
        ("gh issue list", EndpointClass.OTHER),
        ("GET repos/o/r", EndpointClass.OTHER),
    ],
)
def test_commands_are_charged_to_githubs_rate_limit_resources(
    command: str, expected: EndpointClass
) -> None:
    assert classify_command(command) is expected


def test_calls_between_two_reports_count_only_what_was_spent() -> None:
    before = {"by_command": {"GET /search/issues": 5, "POST /graphql": 1}}
    after = {"by_command": {"GET /search/issues": 35, "POST /graphql": 1, "GET /repos/o/r/pulls": 2}}

    counts = GitHubCallCounts.between(before, after)

    assert counts.count(EndpointClass.SEARCH) == 30
    assert counts.count(EndpointClass.GRAPHQL) == 0
    assert counts.count(EndpointClass.CORE) == 2
    assert counts.total == 32
    assert counts.top_commands == (("GET /search/issues", 30), ("GET /repos/o/r/pulls", 2))


def test_reports_from_different_processes_are_refused() -> None:
    with pytest.raises(ValueError, match="went backwards"):
        GitHubCallCounts.between(
            {"by_command": {"POST /graphql": 9}}, {"by_command": {"POST /graphql": 1}}
        )


def test_malformed_report_is_refused() -> None:
    with pytest.raises(ValueError, match="no by_command"):
        GitHubCallCounts.between(None, {"total_calls": 3})
    with pytest.raises(ValueError, match="malformed"):
        GitHubCallCounts.between(None, {"by_command": {"POST /graphql": "3"}})


@pytest.mark.parametrize(
    ("state", "draft", "merged", "expected"),
    [
        ("open", True, False, PullRequestState.DRAFT),
        ("open", False, False, PullRequestState.READY),
        ("closed", None, False, PullRequestState.CLOSED_UNMERGED),
        ("closed", False, True, PullRequestState.MERGED),
        ("merged", None, False, PullRequestState.MERGED),
    ],
)
def test_pull_request_state(state: str, draft: bool | None, merged: bool, expected: PullRequestState) -> None:
    assert PullRequestState.from_github(state=state, draft=draft, merged=merged) is expected


def test_open_pull_request_without_a_draft_flag_is_refused() -> None:
    with pytest.raises(ValueError, match="draft flag"):
        PullRequestState.from_github(state="open", draft=None, merged=False)


def test_a_work_item_with_two_open_prs_is_refused() -> None:
    subject = item(prs=(pr(number=1), pr(number=2)))
    with pytest.raises(ValueError, match="2 open pull requests"):
        _ = subject.open_pull_request


def _event(name: str, at: str = "t", **payload: object) -> ItemEvent:
    return ItemEvent(name=name, at=at, payload=payload)


def test_stall_names_the_last_transition_ignoring_housekeeping() -> None:
    events = [
        _event("session.completed", "t1"),
        _event("review_exchange.completed", "t2"),
        _event("tick.completed", "t3"),
        _event("labels.mutation_summary", "t4"),
    ]

    facts = stall_facts(
        events,
        refusing_gate="review_validity:issue_blocked (blocked-failed)",
        blocking_labels=["blocked-failed", "recovery-pending"],
    )

    assert facts.last_transition == "review_exchange.completed"
    assert facts.last_transition_at == "t2"
    assert facts.blocking_labels == ("blocked-failed", "recovery-pending")
    assert facts.refusing_gate == "review_validity:issue_blocked (blocked-failed)"
    assert facts.unanswered_screen == ""


def test_an_agent_that_never_took_its_prompt_is_an_unanswered_screen() -> None:
    """The Codex 0.156 "Folder access" dialog surfaced exactly like this."""
    events = [
        _event("review_exchange.role_timeout", role="reviewer", failure_reason="prompt_not_accepted",
               composer_state="undetermined"),
        _event("review_exchange.role_timeout", role="reviewer", failure_reason="process_exited_before_response"),
    ]

    assert unanswered_screen(events) == "reviewer: prompt_not_accepted, composer undetermined"


def test_a_process_that_simply_exited_is_not_an_unanswered_screen() -> None:
    events = [_event("review_exchange.role_timeout", role="reviewer",
                     failure_reason="process_exited_before_response")]

    assert unanswered_screen(events) == ""


def test_stream_events_without_a_type_are_refused() -> None:
    with pytest.raises(ValueError, match="without a type"):
        ItemEvent.from_stream({"payload": {}})
    parsed = ItemEvent.from_stream({"type": "session.started", "payload": {"timestamp": "t9"}})
    assert (parsed.name, parsed.at) == ("session.started", "t9")
