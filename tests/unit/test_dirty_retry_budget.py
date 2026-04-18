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

    def test_saved_file_is_valid_json(self, worktree: Path) -> None:
        """Atomic write (tempfile + rename) must still land a well-formed
        JSON file on disk — the rename is only atomic if the tempfile
        contents are complete before the swap."""
        record_rejection(worktree, "sess-a")
        record_rejection(worktree, "sess-b")

        raw = (worktree / COUNTER_RELATIVE_PATH).read_text()
        assert json.loads(raw) == {"sess-a": 1, "sess-b": 1}

    def test_atomic_write_leaves_no_tempfile_on_success(
        self, worktree: Path
    ) -> None:
        """After a normal write, only the canonical counter file
        should remain in the runtime-metadata dir. An abandoned tempfile
        would accumulate over a long-lived session."""
        record_rejection(worktree, "sess-a")

        parent = (worktree / COUNTER_RELATIVE_PATH).parent
        files = sorted(p.name for p in parent.iterdir())
        assert files == [COUNTER_RELATIVE_PATH.name]


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
        # The body has 20 preview entries, each containing ``file_``
        # exactly once; no other ``file_`` appears in the template.
        # Tight equality pins the preview-limit constant rather than
        # allowing it to drift unnoticed.
        assert payload.comment_body.count("file_") == 20

    def test_short_dirty_file_list_is_not_truncated(self) -> None:
        payload = build_escalation_payload(
            session_id="sess-a",
            dirty_files=["M a.py", "M b.py"],
            count=2,
        )

        assert "more" not in payload.context
        assert "a.py" in payload.context and "b.py" in payload.context


class TestPostEscalationReinvocation:
    """Semantics of calling ``coding-done`` again after an escalation.

    The ``coding-done`` main path resets the counter immediately after
    writing the needs_human record. A subsequent invocation should
    therefore start from a fresh counter (new rejection streak), not
    continue the prior one. This documents the chosen semantic
    (concern #4 on the review for #5953): re-invocation restarts the
    budget rather than immediately re-escalating.
    """

    def test_post_escalation_counter_starts_fresh(self, worktree: Path) -> None:
        session_id = "sess-a"
        # First rejection streak → budget exhaustion.
        assert record_rejection(worktree, session_id) == 1
        assert record_rejection(worktree, session_id) == 2
        assert is_budget_exhausted(2)

        # ``coding_done`` resets the counter in the escalation path
        # immediately after writing the needs_human record.
        reset_rejection_counter(worktree, session_id)

        # Next dirty call starts a fresh streak; must not see the
        # prior count.
        assert record_rejection(worktree, session_id) == 1
        assert not is_budget_exhausted(1)

    def test_post_escalation_eventual_re_escalation(
        self, worktree: Path
    ) -> None:
        """If the tree stays dirty and the agent keeps retrying after
        an escalation, the counter does re-exhaust — same orchestrator
        escalation fires again. Not catastrophic; each escalation
        writes a new completion record (``write_completion_record``
        adds numeric suffixes on collision), so no completion is lost.
        """
        session_id = "sess-a"
        record_rejection(worktree, session_id)
        record_rejection(worktree, session_id)
        reset_rejection_counter(worktree, session_id)

        # Replay the streak.
        record_rejection(worktree, session_id)
        assert record_rejection(worktree, session_id) == 2
        assert is_budget_exhausted(2)


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
