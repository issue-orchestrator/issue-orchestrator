"""An agent parked on a screen is reported from its own terminal recording.

The fixture is the real recording of the tech lead the exam's first Case B
run launched: Claude Code 2.1.x opened on "Quick safety check" with
"No, exit" highlighted and stayed there, with no engine event and no log.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from issue_orchestrator.testing.exam import grade, render_summary
from issue_orchestrator.testing.exam.cases import (
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    blocked_issue_green_pr_awaiting_review,
)
from issue_orchestrator.testing.exam.screens import render_recording, silent_screen
from issue_orchestrator.testing.exam.stall import ItemEvent, stall_facts

from .builders import item, observation, pr

FIXTURE = Path(__file__).parent / "fixtures" / "claude-trust-dialog-recording.jsonl"


def _screen():
    return render_recording(FIXTURE.read_text(encoding="utf-8").splitlines())


def test_the_recording_renders_the_dialog_the_agent_was_parked_on() -> None:
    screen = _screen()

    assert "quick safety check" in screen.text
    assert "❯ no, exit yes, i trust this folder" in screen.text
    assert screen.last_output_offset_ms > 0


def test_only_a_long_silence_is_a_parked_screen() -> None:
    screen = _screen()

    assert silent_screen("issue-7313", screen, silent_seconds=30) == ""
    line = silent_screen("issue-7313", screen, silent_seconds=2100)
    assert line.startswith("issue-7313 silent 2100s on screen: ")
    assert "no, exit" in line


def test_a_recording_that_never_sized_a_terminal_is_refused() -> None:
    frame = {"event_type": "output", "offset_ms": 1, "data_b64": base64.b64encode(b"x").decode()}
    with pytest.raises(ValueError, match="output before any resize"):
        render_recording([json.dumps(frame)])
    with pytest.raises(ValueError, match="no resize event"):
        render_recording([])


def test_round_history_and_a_live_parked_screen_are_kept_apart() -> None:
    events = [ItemEvent("review_exchange.role_timeout", "t", {"role": "reviewer", "failure_reason": "prompt_not_accepted"})]

    facts = stall_facts(events, refusing_gate="", blocking_labels=(), parked_screen="issue-1 silent 300s on screen: 'x'")
    assert facts.unanswered_screen == "reviewer never took its prompt (prompt_not_accepted)"
    assert facts.parked_screen == "issue-1 silent 300s on screen: 'x'"


def test_a_finished_item_with_only_round_history_is_not_a_stall() -> None:
    """Seen live: Case A passed, yet its planted reviewer fault (a round the
    engine moved past) listed the finished item as stalled."""
    from dataclasses import replace

    from issue_orchestrator.testing.exam.cases import (
        HALTED_EXCHANGE_WITH_VALIDATED_WORK,
        halted_exchange_with_validated_work,
    )
    from issue_orchestrator.testing.exam import PullRequestState

    done = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))
    history = replace(done, stall=replace(done.stall, unanswered_screen="reviewer never took its prompt (prompt_write_failed)"))
    card = grade(
        halted_exchange_with_validated_work(
            code_reviewed_label="code-reviewed", blocked_failed_label="blocked-failed", needs_human_label="needs-human"
        ),
        observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, history),
    )
    assert card.passed and card.stalls == ()


def test_an_item_parked_on_a_screen_is_reported_even_when_its_goals_held() -> None:
    from dataclasses import replace

    subject = item(issue_labels=("blocked-failed",), prs=(pr(),))
    parked = replace(subject, stall=replace(subject.stall, parked_screen="issue-901 silent 2100s on screen: 'no, exit'"))
    card = grade(
        blocked_issue_green_pr_awaiting_review(blocked_failed_label="blocked-failed"),
        observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, parked),
    )

    assert all(goal.passed for goal in card.goals)
    assert [stall.role for stall in card.stalls] == ["subject"]
    assert "parked on screen now: issue-901 silent 2100s on screen" in render_summary(card)
