"""Persistent-session review-exchange runner: control-flow tests.

Mock-based: monkeypatches ``open_persistent_session`` / ``send_round``
and substitutes a ``_FakePairRegistry`` so each test drives every
round transition deterministically and asserts on emitted events,
chapter sidecar shape, outcome semantics, and pair-lifecycle
transitions (acquire / release).

End-to-end exercise of the real persistent_round_runner is covered in
``test_persistent_round_runner.py``; this file focuses on the
exchange-loop policy on top of it.
"""

from __future__ import annotations

import json
import os
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


class _FakePairRegistry:
    """Fake registry used by every exchange test in this module.

    Records every spawn and release so tests can assert on pair
    lifecycle. ``acquire`` always invokes the spawn callback (no
    caching) which keeps B1 behavior identical to the pre-registry
    world: every test's exchange spawns a fresh fake pair, releases
    at the end. B2 will exercise the cache-hit path with separate
    coverage that asserts spawn was *not* called.
    """

    def __init__(self) -> None:
        self.acquired: list[Any] = []
        self.released: list[tuple[Any, str]] = []
        self.shutdowns: list[str] = []

    def acquire(self, *, issue_key, spawn):  # noqa: ANN001, ANN201
        pair = spawn()
        self.acquired.append((issue_key, pair))
        return pair

    def release(self, issue_key, *, reason):  # noqa: ANN001, ANN201
        self.released.append((issue_key, reason))

    def shutdown_all(self, *, reason):  # noqa: ANN001, ANN201
        self.shutdowns.append(reason)


def _patch_persistent_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_script: dict[str, list[dict[str, Any] | Exception]],
    write_recording: bool = True,
    write_coder_completion: bool = True,
) -> dict[str, Any]:
    """Patch the runner functions in pse to consume a per-role response script.

    Each ``send_round`` call pops the head off the role's queue. If it's
    a dict, the function returns it; if it's an exception, it raises.
    The opened session also gets a recording file written to make
    ``recording_event_count`` happy (or skipped via flag).

    When ``write_coder_completion`` is True (default), each successful
    coder send_round also writes a stub ``completion-coder.json`` so the
    coder protocol guardrail finds the artifact it expects. Tests that
    want to exercise the missing-completion path pass False.
    """
    registry = _FakePairRegistry()
    state: dict[str, Any] = {
        "opened": [], "rounds_seen": [],
        "run_dir": None,
        "registry": registry,
    }

    def _open(*, command, working_dir, env, recording_path=None,
              additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
        # Discriminate by the worktree's basename only — the full path may
        # contain ``reviewer`` because pytest names tmp dirs after the
        # test, which would mislabel both sessions.
        role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
        if write_recording and recording_path is not None:
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            # Seed with one canonical TerminalRecordingEvent so the strict
            # validation in recording_event_count is satisfied.
            recording_path.write_text(
                '{"schema_version":1,"event_type":"resize","offset_ms":0,'
                '"rows":40,"cols":120}\n',
                encoding="utf-8",
            )
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
        # Stub the coder's completion artifact write so the protocol
        # guardrail in pse._validate_coder_completion has the file it expects.
        if role == "coder" and write_coder_completion:
            run_dir = response_file.parent.parent
            state["run_dir"] = run_dir
            completion = run_dir / "coder" / "completion-coder.json"
            completion.parent.mkdir(parents=True, exist_ok=True)
            completion.write_text(
                json.dumps({"outcome": "completed", "implementation": "stub"}),
                encoding="utf-8",
            )
            # Also stub a passing validation-record.json by default so
            # require_validation=True tests don't blow up on the coder
            # guardrail. Tests exercising missing-validation set
            # ``write_coder_completion=False``.
            (run_dir / "validation-record.json").write_text(
                json.dumps({"passed": True}), encoding="utf-8",
            )
        return head

    monkeypatch.setattr(pse, "open_persistent_session", _open)
    monkeypatch.setattr(pse, "send_round", _send)
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

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "Looks good", "getting_closer": True}],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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
        # Both sessions opened. The pair is NOT released at exchange
        # end — B2 ADR 0026 keeps the pair alive across exchanges so
        # the issue can run multiple back-to-back exchanges with the
        # same coder + reviewer processes.
        assert sorted(state["opened"]) == ["coder", "reviewer"]
        assert state["registry"].released == []
        assert len(state["registry"].acquired) == 1


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

        state = _patch_persistent_runner(
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
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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

    def test_reviewer_ok_overridden_when_validation_required_but_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reviewer returns ok in round 1 but validation-record.json is missing
        — the loop coerces the reviewer's verdict to changes_requested and
        continues into the coder turn. The coder also can't satisfy the
        protocol (no validation record possible), so retries exhaust and the
        outcome is coder_protocol_error. The point this test pins is that
        the coder turn WAS attempted: the coercion fired and the exchange
        did not return ``status=ok`` from the reviewer's optimistic verdict.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # write_coder_completion=False: the helper does NOT write
        # completion-coder.json or a passing validation record, so the
        # coder protocol guardrail will fail on every send_round and
        # retries will run.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "ok", "response_text": "lgtm", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "x", "getting_closer": None},
                    {"response_type": "ok", "response_text": "y", "getting_closer": None},
                    {"response_type": "ok", "response_text": "z", "getting_closer": None},
                ],
            },
            write_coder_completion=False,
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=2,
            max_no_progress=5,
            require_validation=True,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # And the coder turn was indeed attempted — proves the reviewer
        # coercion fired (otherwise the exchange would have returned ok).
        coder_calls = [r for role, _ in state["rounds_seen"] if (r := role) == "coder"]
        assert coder_calls, "coder turn must have been attempted"

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
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("simulated timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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

        state = _patch_persistent_runner(
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
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "Approved", "getting_closer": True}],
                "coder": [],
            },
        )

        pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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

    def test_chapter_recorded_event_emitted_per_chapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Original PR 2c plan item 4 contract: chapters.json gets a sidecar
        AND ``REVIEW_EXCHANGE_CHAPTER_RECORDED`` fires for each chapter so
        SSE/timeline consumers can react to round/role/section boundaries
        without polling the on-disk file."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested",
                     "response_text": "Needs work",
                     "getting_closer": True},
                    {"response_type": "ok",
                     "response_text": "Approved",
                     "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "Applied"},
                ],
            },
        )

        pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=2,
            max_no_progress=3,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        chapter_events = [
            evt for evt in sink.events
            if evt.event_type == EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED
        ]
        # Two rounds × (reviewer prompt + reviewer feedback) = 4 reviewer chapters.
        # Round 1 also has (coder prompt + coder feedback) = 2 more. Round 2's
        # reviewer says "ok" so coder doesn't run again. Total = 6.
        assert len(chapter_events) >= 4, (
            f"expected at least 4 chapter events (2 rounds × reviewer prompt+feedback); "
            f"got {len(chapter_events)}"
        )
        # Each event carries the offset that was just durably written, so
        # consumers don't have to re-read the recording to learn where the
        # chapter starts.
        for evt in chapter_events:
            assert "recording_event_index" in evt.data
            assert "role" in evt.data and evt.data["role"] in {"reviewer", "coder"}
            assert "section" in evt.data
            assert "round_index" in evt.data
            assert "label" in evt.data


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

        state = _patch_persistent_runner(
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
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "lgtm", "getting_closer": True}],
                "coder": [],
            },
        )

        observed: list[Path] = []
        pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
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


class TestCoderProtocolGuardrail:
    """Parity with the active runner's _validate_coder_protocol: the coder
    must produce completion-coder.json (output of ``coding-done``) or the
    round is treated as a protocol error and retried."""

    def test_missing_coder_completion_triggers_retries_then_protocol_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        # No completion artifact gets written (write_coder_completion=False).
        # 1 initial coder round + _CODER_PROTOCOL_RETRY_LIMIT (=2) retries
        # = 3 send_round calls before the runner gives up.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "fix",
                     "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "x", "getting_closer": None},
                    {"response_type": "ok", "response_text": "y", "getting_closer": None},
                    {"response_type": "ok", "response_text": "z", "getting_closer": None},
                ],
            },
            write_coder_completion=False,
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # 3 coder send_rounds happened (1 initial + 2 retries).
        coder_rounds = [r for r in state["rounds_seen"] if r[0] == "coder"]
        assert len(coder_rounds) == 3
        # Terminal event fired with status=error so timeline consumers see
        # the exchange ended definitively.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
            and evt.data.get("status") == "error"
            and evt.data.get("reason") == "coder_protocol_error"
            for evt in sink.events
        )

    def test_stale_round1_completion_does_not_satisfy_round2_guardrail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1's coder writes completion-coder.json. Round 2's coder
        does NOT refresh it. The runner must clear the stale artifact
        before round 2's prompt and detect the missing fresh write — i.e.
        the exchange ends in coder_protocol_error, not status=ok with a
        stale satisfaction.

        Regression for the PR 6145 reviewer's repro: previously round 2
        re-used round 1's completion file and the exchange wrongly
        approved.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        # Manually drive the helper: round 1 coder gets a completion
        # file, round 2 coder explicitly skips writing one. We do this
        # by patching ``send_round`` ourselves so we can vary the
        # behavior turn-by-turn.
        from issue_orchestrator.execution import (
            persistent_session_exchange as pse_mod,
        )
        coder_call_count = {"n": 0}

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
            if recording_path is not None:
                recording_path.parent.mkdir(parents=True, exist_ok=True)
                recording_path.write_text(
                    '{"schema_version":1,"event_type":"resize",'
                    '"offset_ms":0,"rows":40,"cols":120}\n',
                    encoding="utf-8",
                )
            return _FakeSession(role)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            if role == "coder":
                coder_call_count["n"] += 1
                run_dir = response_file.parent.parent
                if coder_call_count["n"] == 1:
                    # Round 1: write a completion + passing validation.
                    (run_dir / "coder" / "completion-coder.json").write_text(
                        json.dumps({"outcome": "completed", "round": 1}),
                        encoding="utf-8",
                    )
                    (run_dir / "validation-record.json").write_text(
                        json.dumps({"passed": True}), encoding="utf-8",
                    )
                # Round 2 and retries: do NOT write completion. The runner
                # must have cleared the round-1 file before sending this
                # prompt, so _validate_coder_completion will fail.
                return {"response_type": "ok", "response_text": "x", "getting_closer": None}
            # Reviewer side: round 1 changes_requested, round 2 ok.
            if not state["reviewer_responses"]:
                raise AssertionError("reviewer responses exhausted")
            return state["reviewer_responses"].pop(0)

        # Both reviewer rounds say "changes_requested" so the coder runs
        # in both rounds. (If round 2's reviewer said "ok" the loop would
        # short-circuit before the coder turn and the bug wouldn't be
        # exercised.)
        state = {
            "reviewer_responses": [
                {"response_type": "changes_requested", "response_text": "fix",
                 "getting_closer": True},
                {"response_type": "changes_requested", "response_text": "still fix",
                 "getting_closer": True},
            ],
            "registry": _FakePairRegistry(),
        }
        monkeypatch.setattr(pse_mod, "open_persistent_session", _open)
        monkeypatch.setattr(pse_mod, "send_round", _send)

        outcome = pse_mod.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=3,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.status == "error"
        assert outcome.reason == "coder_protocol_error"
        # Round 1 + round 2 + 2 protocol retries = 4 coder calls.
        assert coder_call_count["n"] == 4

    def test_coder_completion_present_passes_protocol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: when the coder writes completion-coder.json (the helper's
        default), no retry path is taken — only one coder send_round per round."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [
                    {"response_type": "changes_requested", "response_text": "fix",
                     "getting_closer": True},
                    {"response_type": "ok", "response_text": "lgtm", "getting_closer": True},
                ],
                "coder": [
                    {"response_type": "ok", "response_text": "fixed", "getting_closer": None},
                ],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=2,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.status == "ok"
        coder_rounds = [r for r in state["rounds_seen"] if r[0] == "coder"]
        assert len(coder_rounds) == 1


class TestTerminalEventsOnError:
    """Every error/timeout exit path emits a terminal REVIEW_EXCHANGE_COMPLETED
    event so the timeline / publish cache observe a definitive end-of-exchange."""

    def test_reviewer_timeout_emits_terminal_completed_event_with_error_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
            events=sink,
            event_context=ctx,
        )

        assert outcome.status == "error"
        assert outcome.reason == "reviewer_no_completion"
        terminal = [
            evt for evt in sink.events
            if evt.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
        ]
        assert len(terminal) == 1
        assert terminal[0].data.get("status") == "error"
        assert terminal[0].data.get("reason") == "reviewer_no_completion"

    def test_summary_status_matches_outcome_for_error_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """summary.json's ``status`` must match the outcome's ``status``,
        not say "stopped"/"incomplete" while the outcome says "error"."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        from issue_orchestrator.execution.persistent_round_runner import (
            PersistentRoundTimeoutError,
        )
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [PersistentRoundTimeoutError("timeout")],
                "coder": [],
            },
        )

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
        )

        assert outcome.summary is not None
        assert outcome.summary["status"] == "error"
        assert outcome.summary["reason"] == "reviewer_no_completion"


class TestRecordingContractFailLoud:
    """Missing or corrupt recording at chapter time is a broken replay
    contract and must surface as a definitive exchange failure, not a
    silent skipped chapter."""

    def test_missing_recording_at_chapter_time_fails_exchange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        sink = _Sink()
        ctx = EventContext()

        # write_recording=False: helper does NOT seed the recording file,
        # so the first _record_chapter call hits a missing recording and
        # raises FileNotFoundError up the stack.
        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "lgtm",
                              "getting_closer": True}],
                "coder": [],
            },
            write_recording=False,
        )

        with pytest.raises(FileNotFoundError):
            pse.run_persistent_session_exchange(
                session_output=session_output,
                pair_registry=state["registry"],
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                max_rounds=1,
                max_no_progress=5,
                require_validation=False,
                events=sink,
                event_context=ctx,
            )
        # FAILED event fired before the raise propagated.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        )


class TestAtomicSummaryWrite:
    def test_summary_write_is_atomic_no_partial_file_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The orchestrator polls summary.json from a different process
        while the exchange runs in the background. The write must be
        atomic — a torn file would surface as JSONDecodeError on the
        polling tick.

        We verify the contract by patching ``os.replace`` to capture the
        intermediate temp path and asserting that the destination is
        either fully present or absent at any observable moment.
        """
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "lgtm",
                              "getting_closer": True}],
                "coder": [],
            },
        )

        observed_tmp_paths: list[str] = []
        real_replace = os.replace

        def _capturing_replace(src: str, dst: str) -> None:
            observed_tmp_paths.append(src)
            return real_replace(src, dst)

        # Atomic write lives in the shared infra.atomic_io helper now;
        # patch os.replace at that module's binding so we observe the
        # actual rename call from the runner's summary write path.
        from issue_orchestrator.infra import atomic_io
        monkeypatch.setattr(atomic_io.os, "replace", _capturing_replace)

        outcome = pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=state["registry"],
            persistent_pair_root=tmp_path / "persistent-pairs",
            coder_worktree_path=coder_wt,
            reviewer_worktree_factory=lambda: reviewer_wt,
            issue_number=42,
            issue_title="Test",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=_make_agent(prompt_path),
            reviewer_agent=_make_agent(prompt_path),
            max_rounds=1,
            max_no_progress=5,
            require_validation=False,
        )

        # summary.json was written via a tempfile + atomic replace.
        assert observed_tmp_paths, "atomic write was not exercised"
        assert all(str(p).endswith(".tmp") for p in observed_tmp_paths)
        assert outcome.summary is not None


class TestRoleEnvironmentScrubbing:
    """The persistent runner must route the role env through the shared
    ``build_filtered_env`` policy. Without it, long-lived agent processes
    would inherit orchestrator credentials (GH_TOKEN, ISSUE_ORCHESTRATOR_API_TOKEN,
    CLAUDECODE, …) that the active path deliberately scrubs."""

    def test_forbidden_env_vars_are_scrubbed_from_role_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pollute the orchestrator's env with the exact secrets the
        # filtered-env policy guards against.
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", "admin-secret")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-fake")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        response_file = run_dir / "reviewer" / "review-response.json"

        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            response_file=response_file,
            completion_path=run_dir / "reviewer" / "completion-reviewer.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            agent_label="agent:reviewer",
            web_port=None,
            issue_number=4057,
            session_name="exchange-1",
        )

        # Forbidden vars must be absent.
        for forbidden in (
            "GH_TOKEN", "GITHUB_TOKEN", "ISSUE_ORCHESTRATOR_API_TOKEN",
            "CLAUDECODE", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY",
        ):
            assert forbidden not in env, f"{forbidden} leaked into agent env"

    def test_required_overrides_are_present_in_role_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        response_file = run_dir / "coder" / "review-response.json"
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")  # ensure scrubbing path runs

        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            response_file=response_file,
            completion_path=run_dir / "coder" / "completion-coder.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            agent_label="agent:backend",
            web_port=8080,
            issue_number=4057,
            session_name="exchange-1",
        )

        # The orchestrator-side overrides we depend on must propagate.
        assert env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] == str(response_file)
        assert env["ISSUE_ORCHESTRATOR_AGENT_LABEL"] == "agent:backend"
        assert env["ISSUE_ORCHESTRATOR_ISSUE_NUMBER"] == "4057"
        assert env["ORCHESTRATOR_SESSION_ID"] == "exchange-1"
        assert env["ORCHESTRATOR_API_PORT"] == "8080"
        # Git-safe defaults from the filtered-env helper.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # And GH_TOKEN was still scrubbed (sanity).
        assert "GH_TOKEN" not in env

    def test_callback_token_propagates_when_set_in_orchestrator_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ALWAYS_PASSTHROUGH_ENV_VARS must reach the agent so it can call
        back into the loopback Control API for preflight-push / session-resume."""
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", "scoped-cb-token")

        run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        env = pse._build_role_env(  # noqa: SLF001 — testing the env contract
            response_file=run_dir / "reviewer" / "review-response.json",
            completion_path=run_dir / "reviewer" / "completion-reviewer.json",
            validation_output_dir=run_dir,
            worktree=worktree,
            agent_label="agent:reviewer",
            web_port=None,
            issue_number=4057,
            session_name="exchange-1",
        )
        assert env.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN") == "scoped-cb-token"


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
                pair_registry=state["registry"],
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
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

        # B2: a mid-round exception does NOT release the pair — the
        # pair is alive enough for the next exchange to retry the
        # work, and lifecycle release happens at issue-completion /
        # reset / shutdown sites instead. (B1's "release on every
        # finally" assertion was the right invariant for B1's
        # per-exchange ownership; B2 inverts it.)
        assert state["registry"].released == []
        # And the failure event was emitted before raising.
        assert any(
            evt.event_type is EventName.REVIEW_EXCHANGE_FAILED for evt in sink.events
        )


class TestSpawnPartialConstructionCleanup:
    """Regression for PR #6209 review finding: if reviewer spawn fails
    after coder spawn succeeds, the coder PTY/process must be closed
    before the spawn closure raises.

    Pre-registry code wrapped both opens in a single ``try``; the
    registry refactor (PR #6209) initially moved both opens into a
    spawn closure that returned no value on partial failure, leaking
    the coder. The fix wraps the reviewer open in a nested
    ``try/except`` that closes the coder before re-raising. This
    test pins that behavior so the leak doesn't come back.
    """

    def test_coder_session_is_closed_when_reviewer_open_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        opened_sessions: list[_FakeSession] = []
        closed_sessions: list[_FakeSession] = []

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = (
                "reviewer" if Path(working_dir).name.startswith("reviewer-wt")
                else "coder"
            )
            if role == "reviewer":
                # Simulate a reviewer-side failure during PTY/process
                # bring-up, after the coder has already opened.
                raise RuntimeError("reviewer pty bring-up failed")
            session = _FakeSession(role)
            opened_sessions.append(session)
            return session

        def _close(session, **_):
            closed_sessions.append(session)
            session.closed = True
            return 0

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "close_persistent_session", _close)

        registry = _FakePairRegistry()
        with pytest.raises(RuntimeError, match="reviewer pty bring-up failed"):
            pse.run_persistent_session_exchange(
                session_output=session_output,
                pair_registry=registry,
                persistent_pair_root=tmp_path / "persistent-pairs",
                coder_worktree_path=coder_wt,
                reviewer_worktree_factory=lambda: reviewer_wt,
                issue_number=42,
                issue_title="Test",
                coder_label="agent:backend",
                reviewer_label="agent:reviewer",
                coder_agent=_make_agent(prompt_path),
                reviewer_agent=_make_agent(prompt_path),
                max_rounds=1,
                max_no_progress=2,
                require_validation=False,
            )

        # The coder opened, the reviewer raised before opening — and
        # the coder must have been closed by the spawn closure's
        # cleanup ``except`` block. No partial pair must reach the
        # registry's cache.
        assert len(opened_sessions) == 1
        assert opened_sessions[0].role == "coder"
        assert closed_sessions == opened_sessions, (
            "coder session leaked: registry refactor must close any "
            "already-opened session if the partner spawn raises"
        )
        assert registry.acquired == [], (
            "no pair must reach the registry on partial spawn failure"
        )
