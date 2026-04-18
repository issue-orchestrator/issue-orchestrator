"""Tests for the dirty-tree retry budget (#5949 item 2).

Pins the invariants the ``coding-done`` main path depends on: counter
isolation per session, reset on recovery, escalation payload content,
and the budget threshold itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.entrypoints.cli_tools.dirty_retry_budget import (
    COUNTER_RELATIVE_PATH,
    DIRTY_REJECTION_BUDGET,
    build_completion_record_for_escalation,
    build_escalation_payload,
    is_budget_exhausted,
    record_rejection,
    reset_rejection_counter,
)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path


class TestRecordRejection:
    def test_first_rejection_returns_one(self, worktree: Path) -> None:
        assert record_rejection(worktree, "sess-a") == 1

    def test_counter_accumulates_per_session(self, worktree: Path) -> None:
        assert record_rejection(worktree, "sess-a") == 1
        assert record_rejection(worktree, "sess-a") == 2
        assert record_rejection(worktree, "sess-a") == 3

    def test_sessions_are_isolated(self, worktree: Path) -> None:
        """Two concurrent sessions in the same worktree must not share
        a counter — a rejection in one must not push the other over the
        budget."""
        record_rejection(worktree, "sess-a")
        record_rejection(worktree, "sess-a")

        assert record_rejection(worktree, "sess-b") == 1

    def test_counter_file_is_json_under_issue_orchestrator(
        self, worktree: Path
    ) -> None:
        """The file location is part of the contract — it lives in the
        runtime-metadata tree that dirty-tree guards already ignore, so
        its presence can't itself trip the guard we're counting
        rejections of."""
        record_rejection(worktree, "sess-a")
        path = worktree / COUNTER_RELATIVE_PATH

        assert path.exists()
        assert str(COUNTER_RELATIVE_PATH).startswith(".issue-orchestrator/")
        data = json.loads(path.read_text())
        assert data == {"sess-a": 1}

    def test_corrupt_counter_file_recovers(self, worktree: Path) -> None:
        """Partial writes or external tampering must not crash
        ``coding-done`` — the tool treats a corrupt counter as empty
        and continues."""
        path = worktree / COUNTER_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json {{{")

        assert record_rejection(worktree, "sess-a") == 1


class TestResetRejectionCounter:
    def test_clears_only_the_named_session(self, worktree: Path) -> None:
        record_rejection(worktree, "sess-a")
        record_rejection(worktree, "sess-b")

        reset_rejection_counter(worktree, "sess-a")

        assert record_rejection(worktree, "sess-a") == 1  # Starts fresh
        assert record_rejection(worktree, "sess-b") == 2  # Unaffected

    def test_reset_on_only_session_removes_the_file(
        self, worktree: Path
    ) -> None:
        """Once no sessions have a counter, the file should not linger —
        otherwise the runtime-metadata tree accumulates cruft per-session
        indefinitely."""
        record_rejection(worktree, "sess-a")
        path = worktree / COUNTER_RELATIVE_PATH
        assert path.exists()

        reset_rejection_counter(worktree, "sess-a")

        assert not path.exists()

    def test_reset_unknown_session_is_a_noop(self, worktree: Path) -> None:
        """Called on the happy path (dirty check passed) whether or not
        the session ever had a rejection. Must not raise."""
        reset_rejection_counter(worktree, "sess-never-rejected")


class TestBudgetThreshold:
    def test_budget_is_two(self) -> None:
        """Pin the specific budget value so ``coding-done`` behaviour
        changes require deliberately editing the constant and this test
        together."""
        assert DIRTY_REJECTION_BUDGET == 2

    def test_below_budget_returns_false(self) -> None:
        assert not is_budget_exhausted(1)

    def test_at_budget_returns_true(self) -> None:
        assert is_budget_exhausted(2)

    def test_above_budget_returns_true(self) -> None:
        """Defence-in-depth: a race or double-write that pushes the
        counter past the threshold must still trigger escalation — we
        never want a rejection count higher than the budget to silently
        pass."""
        assert is_budget_exhausted(3)
        assert is_budget_exhausted(10)


class TestEscalationPayload:
    def test_explicit_auto_escalation_language(self) -> None:
        """A reviewing human must immediately see this wasn't the
        agent's own judgement call."""
        payload = build_escalation_payload(
            session_id="sess-a",
            dirty_files=["M file.py"],
            count=2,
        )

        assert payload.session_id == "sess-a"
        for text in (payload.question, payload.summary, payload.comment_body):
            assert "auto" in text.lower() or "Auto" in text
        assert "2" in payload.summary

    def test_long_dirty_file_list_is_truncated(self) -> None:
        dirty = [f"?? file_{i}.py" for i in range(50)]
        payload = build_escalation_payload(
            session_id="sess-a", dirty_files=dirty, count=2
        )

        # Preview shows first N with an "and X more" tail; full count
        # is still reported numerically so the human knows the scale.
        assert "50" in payload.context
        assert "... and 30 more" in payload.context
        # Body is Markdown, must not embed the full list either.
        assert payload.comment_body.count("file_") <= 22

    def test_short_dirty_file_list_is_not_truncated(self) -> None:
        payload = build_escalation_payload(
            session_id="sess-a",
            dirty_files=["M a.py", "M b.py"],
            count=2,
        )

        assert "more" not in payload.context
        assert "a.py" in payload.context and "b.py" in payload.context


class TestBuildCompletionRecordForEscalation:
    """The escalation record assembly uses stand-in classes so the test
    doesn't pull in the runtime domain model — that module is heavy and
    the assembly logic is pure mapping."""

    def test_maps_payload_to_completion_record_fields(self) -> None:
        @dataclass
        class FakeRecord:
            session_id: str = ""
            timestamp: str = ""
            outcome: Any = None
            summary: str = ""
            requested_actions: list[Any] = field(default_factory=list)
            question: str | None = None
            context: str | None = None
            options: Any = None
            default_action: Any = None
            comment_body: str = ""

        class FakeOutcome:
            NEEDS_HUMAN = "needs_human"

        status_actions = {"needs_human": ["NEEDS_HUMAN_ACTION"]}
        payload = build_escalation_payload(
            session_id="sess-x",
            dirty_files=["M only.py"],
            count=2,
        )

        record = build_completion_record_for_escalation(
            payload,
            completion_record_cls=FakeRecord,
            completion_outcome_cls=FakeOutcome,
            status_to_actions=status_actions,
            needs_human_status="needs_human",
        )

        assert record.session_id == "sess-x"
        assert record.outcome == "needs_human"
        assert record.summary == payload.summary
        assert record.question == payload.question
        assert record.context == payload.context
        assert record.comment_body == payload.comment_body
        assert record.requested_actions == ["NEEDS_HUMAN_ACTION"]
        assert record.timestamp  # Non-empty ISO timestamp
        # Options/default_action must be cleared — the human picks the
        # path forward, the agent has nothing to suggest.
        assert record.options is None
        assert record.default_action is None
