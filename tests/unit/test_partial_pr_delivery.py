"""A coding agent can declare its PR a partial delivery of the issue (#7288).

porchpin #320 needed one PR per package ("this issue closes when the last one
lands"). Every io PR opened with "Closes #320", so the first merged slice
closed the issue. These tests cover the agent side of that fix and the gate
that decides whether an issue can launch again:

* the agent declares partiality with ``coding-done completed --partial``;
* the completion record carries it as untrusted input, validated strictly;
* the PR body names the issue with "Refs #N", which links it without
  closing it;
* after the partial PR merges, the session-history gate releases the issue so
  its next slice can launch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.control.queue_cache import QueueCache, QueueMutationStatus
from issue_orchestrator.domain.models import (
    AgentConfig,
    CompletionOutcome,
    CompletionRecord,
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.pr_issue_reference import (
    body_links_issue,
    declares_partial_delivery,
    issue_reference_line,
    linked_issue_number,
)
from issue_orchestrator.entrypoints.cli_tools import agent_done, coding_done
from issue_orchestrator.history import issues_held_by_session_history
from issue_orchestrator.infra.config import Config


# --- the reference line -----------------------------------------------------


def test_the_reference_line_closes_a_whole_delivery_and_refs_a_partial_one() -> None:
    assert issue_reference_line(320, partial=False) == "Closes #320"
    assert issue_reference_line(320, partial=True) == "Refs #320"


@pytest.mark.parametrize(
    ("body", "linked"),
    [
        ("Closes #320\n\nbody", 320),
        ("Refs #320\n\nbody", 320),
        # The first link in body order wins: the orchestrator's own line is
        # line 1, so a later "Closes #M" in agent-written text cannot move it.
        ("Refs #320\nCloses #1", 320),
        ("Closes #320\n\nAlso Refs #1", 320),
        ("No reference here, see #320", None),
        # A word-boundary-defeated reference links nothing, as before.
        ("done.\\n\\nCloses #45.", None),
    ],
)
def test_a_body_links_the_issue_it_closes_or_refs(body: str, linked: int | None) -> None:
    assert linked_issue_number(body) == linked
    assert body_links_issue(body, [320]) is (linked == 320)


@pytest.mark.parametrize(
    ("body", "partial"),
    [
        ("Refs #320", True),
        ("refs #320 - deliberately not Closes", True),
        ("Closes #320", False),
        # Any GitHub closing keyword closes the issue on merge, so a body with
        # one is never partial, whatever else it says.
        ("Refs #320\nFixes #320", False),
        ("Refs #320\nresolved: #320", False),
        ("Refs #320\nCloses porchpin/porchpin#320", False),
        # A reference to another issue says nothing about this one.
        ("Refs #321", False),
        ("Refs #320\nCloses #321", True),
        # No reference at all is a broken close, not a declared partial.
        ("", False),
    ],
)
def test_partial_delivery_is_a_refs_line_with_no_closing_keyword(
    body: str, partial: bool
) -> None:
    assert declares_partial_delivery(body, 320) is partial


# --- the completion record --------------------------------------------------


def _record_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "session_id": "s",
        "timestamp": "2026-09-26T00:00:00Z",
        "outcome": "completed",
        "summary": "done",
        "requested_actions": ["push_branch", "create_pr"],
    }
    data.update(overrides)
    return data


def test_the_record_round_trips_a_partial_claim_and_defaults_to_whole() -> None:
    partial = CompletionRecord.from_dict(_record_data(partial_pr=True))
    assert partial.partial_pr is True
    assert CompletionRecord.from_dict(partial.to_dict()).partial_pr is True
    # An older agent-side runtime writes no field: that is a whole delivery.
    assert CompletionRecord.from_dict(_record_data()).partial_pr is False


@pytest.mark.parametrize("value", ["true", 1, None])
def test_the_record_refuses_a_partial_claim_that_is_not_a_bool(value: object) -> None:
    with pytest.raises(ValueError, match="partial_pr must be a boolean"):
        CompletionRecord.from_dict(_record_data(partial_pr=value))


def test_only_a_completion_can_claim_partial_delivery() -> None:
    with pytest.raises(ValueError, match="only valid for a completed outcome"):
        CompletionRecord.from_dict(
            _record_data(outcome="blocked", partial_pr=True, requested_actions=[])
        )


# --- the coding-done CLI ----------------------------------------------------


def _completed_args(*extra: str) -> argparse.Namespace:
    return coding_done.build_parser().parse_args(
        ["completed", "--implementation", "slice A", "--problems", "None", *extra]
    )


def test_coding_done_partial_reaches_the_record_the_orchestrator_reads() -> None:
    """The runtime record the agent writes is the domain record's input: the
    flag must survive to_dict and the orchestrator's validating from_dict."""
    args = _completed_args("--partial")
    agent_done.validate_fields("completed", args)
    with patch.object(agent_done, "get_session_id", return_value="s"):
        written = agent_done.build_completion_record("completed", args)

    read = CompletionRecord.from_dict(json.loads(json.dumps(written.to_dict())))

    assert read.outcome is CompletionOutcome.COMPLETED
    assert read.partial_pr is True


def test_coding_done_without_partial_records_a_whole_delivery() -> None:
    args = _completed_args()
    with patch.object(agent_done, "get_session_id", return_value="s"):
        written = agent_done.build_completion_record("completed", args)

    assert CompletionRecord.from_dict(written.to_dict()).partial_pr is False


@pytest.mark.parametrize(
    ("implementation", "refused"),
    [
        ("Split package A. Fixes #320 once B lands.", True),
        ("Split package A. Refs #320.", False),
        # Another issue's closing keyword does not close this one.
        ("Split package A. Closes #321.", False),
    ],
)
def test_coding_done_partial_refuses_text_that_would_close_the_issue(
    monkeypatch: pytest.MonkeyPatch, implementation: str, refused: bool
) -> None:
    """#7288 R2: the implementation text goes into the PR body, and a closing
    keyword there closes the issue on merge despite "Refs". The agent learns
    this while it can still reword, not after publication is refused."""
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "320")
    args = coding_done.build_parser().parse_args(
        ["completed", "--implementation", implementation, "--problems", "None", "--partial"]
    )

    if refused:
        with pytest.raises(SystemExit):
            agent_done.validate_fields("completed", args)
    else:
        agent_done.validate_fields("completed", args)


def test_coding_done_refuses_partial_on_an_escalation() -> None:
    args = coding_done.build_parser().parse_args(
        ["blocked", "--reason", "r", "--attempted", "a", "--partial"]
    )
    with pytest.raises(SystemExit):
        agent_done.validate_fields("blocked", args)


# --- the session-history gate -----------------------------------------------


def _entry(number: int, *, status: str = "completed", partial: bool = False) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=number,
        title=f"Issue {number}",
        agent_type="agent:web",
        status=status,  # type: ignore[arg-type]
        runtime_minutes=1,
        pr_url=f"https://github.com/owner/repo/pull/{900 + number}",
        partial_pr_merged=partial,
    )


def test_the_gate_releases_an_issue_whose_latest_entry_is_a_partial_merge() -> None:
    history = [
        _entry(1, status="merged"),
        _entry(2, status="merged", partial=True),
        # The next slice of #3 launched after its partial merge: held again.
        _entry(3, status="merged", partial=True),
        _entry(3),
    ]

    assert issues_held_by_session_history(history) == frozenset({1, 3})


def test_the_queue_cache_admits_an_issue_released_by_a_partial_merge() -> None:
    """The planner and the queue cache ask the same owner, so the issue's next
    slice is neither dropped from the cache nor skipped by the planner."""
    config = Config(
        repo="owner/repo",
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp/worktrees"),
        agents={"agent:web": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )
    state = OrchestratorState(
        session_history=[_entry(7, status="merged", partial=True), _entry(8, status="merged")]
    )
    cache = QueueCache(config, state)

    assert cache.evaluate_issue(Issue(7, "Issue 7", ["agent:web"])) == QueueMutationStatus.ACCEPTED
    assert cache.evaluate_issue(Issue(8, "Issue 8", ["agent:web"])) == QueueMutationStatus.REJECTED_EXCLUDED
