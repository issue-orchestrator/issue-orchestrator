"""Persistent-session review-exchange runner: control-flow tests.

Mock-based: monkeypatches ``open_persistent_session`` / ``send_round`` /
``close_persistent_session`` so the test drives every round transition
deterministically and asserts on emitted events, chapter sidecar shape,
and outcome semantics.

End-to-end exercise of the real persistent_round_runner is covered in
``test_persistent_round_runner.py``; this file focuses on the
exchange-loop policy on top of it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.execution import persistent_session_exchange as pse
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports import TraceEvent


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class _FakeSession:
    """Mock PersistentSession passed back to the exchange runner."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.closed = False
        self.proc = SimpleNamespace(returncode=None, pid=12345, poll=lambda: None)
        self.master_fd = -1


def _make_agent(prompt_path: Path) -> AgentConfig:
    return AgentConfig(prompt_path=prompt_path, ai_system="claude-code", timeout_minutes=1)


def _setup_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    coder = tmp_path / "coder-wt"
    reviewer = tmp_path / "reviewer-wt"
    coder.mkdir()
    reviewer.mkdir()
    return coder, reviewer


def _patch_persistent_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_script: dict[str, list[dict[str, Any] | Exception]],
    write_recording: bool = True,
) -> dict[str, Any]:
    """Patch the runner functions in pse to consume a per-role response script.

    Each ``send_round`` call pops the head off the role's queue. If it's
    a dict, the function returns it; if it's an exception, it raises.
    The opened session also gets a recording file written to make
    ``recording_event_count`` happy (or skipped via flag).
    """
    state: dict[str, Any] = {"opened": [], "rounds_seen": [], "closed": []}

    def _open(*, command, working_dir, env, recording_path=None,
              additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
        # Discriminate by the worktree's basename only — the full path may
        # contain ``reviewer`` because pytest names tmp dirs after the
        # test, which would mislabel both sessions.
        role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
        if write_recording and recording_path is not None:
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            # Seed with one event so recording_event_count() returns >=1.
            recording_path.write_text('{"event_type":"resize"}\n', encoding="utf-8")
        state["opened"].append(role)
        return _FakeSession(role)

    def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
        role = session.role
        state["rounds_seen"].append((role, prompt[:40]))
        if not response_script.get(role):
            raise AssertionError(f"send_round called for {role} with no scripted response left")
        head = response_script[role].pop(0)
        if isinstance(head, Exception):
            raise head
        return head

    def _close(session, **_):
        state["closed"].append(session.role)
        session.closed = True
        return 0

    monkeypatch.setattr(pse, "open_persistent_session", _open)
    monkeypatch.setattr(pse, "send_round", _send)
    monkeypatch.setattr(pse, "close_persistent_session", _close)
    return state


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestPersistentSessionExchangeHappyPath:
    def test_one_round_reviewer_ok_returns_status_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "Looks good", "getting_closer": True}],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "ok"
        assert outcome.rounds == 1
        assert outcome.reason == "reviewer_ok"
        assert outcome.reviewer_response is not None
        assert outcome.reviewer_response.response_type == "ok"

    def test_two_round_exchange_changes_then_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "Fix typo", "getting_closer": True},
                    {"response_type": "ok", "response_text": "All good", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Fixed typo", "getting_closer": None},
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )
        assert outcome.status == "ok"
        assert outcome.rounds == 2
        # Round order: reviewer round 1 → coder round 1 → reviewer round 2.
        assert [role for role, _ in state["rounds_seen"]] == ["reviewer", "coder", "reviewer"]
        # Both sessions opened and closed.
        assert sorted(state["opened"]) == ["coder", "reviewer"]
        assert sorted(state["closed"]) == ["coder", "reviewer"]


# ---------------------------------------------------------------------------
# Termination conditions
# ---------------------------------------------------------------------------


class TestExchangeTerminationConditions:
    def test_max_no_progress_stops_exchange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "Keep trying", "getting_closer": False},
                    {"response_type": "changes_requested", "response_text": "Still bad", "getting_closer": False},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Tried", "getting_closer": None},
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=5,
            max_no_progress=2,
            require_validation=False,
        )

        assert outcome.status == "stopped"
        assert outcome.reason == "reviewer_reports_no_progress"

    def test_validation_gate_overrides_reviewer_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reviewer returns ok but validation-record.json is missing → coerced to
        changes_requested, exchange continues to next round (or hits max)."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "ok", "response_text": "lgtm", "getting_closer": True},
                    {"response_type": "ok", "response_text": "lgtm again", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Done r1", "getting_closer": None},
                    {"response_type": "ok", "response_text": "Done r2", "getting_closer": None},
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=2,
            max_no_progress=5,
            require_validation=True,  # <-- validation record never written
        )

        # Round 1's "ok" is overridden to changes_requested; round 2's "ok"
        # is also overridden, exchange runs out of rounds. Status is
        # "stopped" with reason max_rounds_exceeded — the gate did its job.
        assert outcome.status == "stopped"
        assert outcome.reason == "max_rounds_exceeded"

    def test_reviewer_timeout_exits_with_no_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        from issue_orchestrator.execution.persistent_round_runner import PersistentRoundTimeoutError
        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("simulated timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )
        assert outcome.status == "error"
        assert outcome.reason == "reviewer_no_completion"
        # Role timeout event must fire so the timeline can render the
        # per-role bailout as a failure, not as silent stoppage.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
            and evt.data.get("role") == "reviewer"
            for evt in sink.events
        )


# ---------------------------------------------------------------------------
# Chapters + events vocabulary
# ---------------------------------------------------------------------------


class TestChapterSidecarAndEvents:
    def test_chapters_recorded_at_each_role_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "Fix", "getting_closer": True},
                    {"response_type": "ok", "response_text": "Approved", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Fixed", "getting_closer": None},
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        # Reviewer side: 2 round-prompt chapters + 2 round-feedback chapters
        # Coder side: 1 round-prompt chapter + 1 round-feedback chapter
        run_dir = outcome.exchange_dir.parent  # type: ignore[union-attr]
        reviewer_sidecar = session_output.read_exchange_chapters(run_dir, role="reviewer")
        coder_sidecar = session_output.read_exchange_chapters(run_dir, role="coder")

        assert reviewer_sidecar is not None
        assert coder_sidecar is not None
        # Reviewer wrote 4 chapters (prompt+feedback × 2 rounds).
        reviewer_pattern = [(c.cycle_index, c.section) for c in reviewer_sidecar.chapters]
        assert reviewer_pattern == [
            (1, "prompt"), (1, "feedback"),
            (2, "prompt"), (2, "feedback"),
        ]
        # Coder wrote 2 chapters (prompt+feedback × 1 round; round 2 reviewer
        # approved, no coder turn).
        coder_pattern = [(c.cycle_index, c.section) for c in coder_sidecar.chapters]
        assert coder_pattern == [(1, "prompt"), (1, "feedback")]

    def test_role_events_emit_in_expected_sequence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "Approved", "getting_closer": True}],
                "coder": [],
            },
        )

        pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        names = [evt.event_type for evt in sink.events]
        # Expected sequence (subset assertion to avoid coupling to extras):
        for expected in (
            EventName.REVIEW_EXCHANGE_STARTED,
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            EventName.REVIEW_EXCHANGE_COMPLETED,
        ):
            assert expected in names, f"missing event {expected.value}"


# ---------------------------------------------------------------------------
# Caller-injected hooks
# ---------------------------------------------------------------------------


class TestCallerHooks:
    def test_before_reviewer_round_invoked_each_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "fix", "getting_closer": True},
                    {"response_type": "ok", "response_text": "lgtm", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "fixed", "getting_closer": None},
                ],
            },
        )

        round_invocations: list[int] = []
        pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
            before_reviewer_round=lambda i: round_invocations.append(i),
        )
        # Called once per reviewer round (rounds 1 and 2).
        assert round_invocations == [1, 2]

    def test_on_started_called_with_run_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "lgtm", "getting_closer": True}],
                "coder": [],
            },
        )

        observed: list[Path] = []
        pse.run_persistent_session_exchange(
            session_output=session_output,
            coder_worktree_path=coder_wt,
            reviewer_worktree_path=reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=1,
            max_no_progress=2,
            require_validation=False,
            on_started=lambda d: observed.append(d),
        )
        assert len(observed) == 1
        assert observed[0].exists()
        # And the run dir contains the exchange artifacts.
        assert (observed[0] / "review-exchange").is_dir()


# ---------------------------------------------------------------------------
# Cleanup invariant
# ---------------------------------------------------------------------------


class TestSessionCleanup:
    def test_sessions_closed_even_on_round_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # Reviewer round 1 raises a non-runner-typed exception (not timeout/error).
        # The exchange must surface REVIEW_EXCHANGE_FAILED and still close both sessions.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [RuntimeError("unexpected")],
                "coder": [],
            },
        )
        sink = _Sink()
        ctx = EventContext()

        with pytest.raises(RuntimeError, match="unexpected"):
            pse.run_persistent_session_exchange(
                session_output=session_output,
                coder_worktree_path=coder_wt,
                reviewer_worktree_path=reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
                events=sink,
                event_context=ctx,
            )

        # Both sessions must be closed.
        assert sorted(state["closed"]) == ["coder", "reviewer"]
        # And the failure event was emitted before raising.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        )
