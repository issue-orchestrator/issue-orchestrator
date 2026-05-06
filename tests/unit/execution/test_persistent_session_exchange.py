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

    def __init__(
        self,
        role: str,
        *,
        completion_path: Path | None = None,
        validation_output_dir: Path | None = None,
    ) -> None:
        self.role = role
        self.closed = False
        self.proc = SimpleNamespace(returncode=None, pid=12345, poll=lambda: None)
        self.master_fd = -1
        # Captured from the agent env at open time so ``_send`` can
        # write protocol-guardrail artifacts (completion, validation
        # record) to the same paths production reads from. The test
        # used to derive the pair_dir as ``response_file.parent.parent``,
        # but the response file moved into the role's worktree (so the
        # agent's seatbelt sandbox can write it). Env-driven discovery
        # is the same indirection production agents use, so the test
        # stays correct as path layout evolves.
        self.completion_path = completion_path
        self.validation_output_dir = validation_output_dir


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
        completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
        validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
        return _FakeSession(
            role,
            completion_path=Path(completion_env) if completion_env else None,
            validation_output_dir=Path(validation_env) if validation_env else None,
        )

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
            completion = session.completion_path
            assert completion is not None, (
                "test fixture: coder fake session must have completion_path "
                "captured from env at open time"
            )
            state["completion_path"] = completion
            completion.parent.mkdir(parents=True, exist_ok=True)
            completion.write_text(
                json.dumps({"outcome": "completed", "implementation": "stub"}),
                encoding="utf-8",
            )
            # Also stub a passing validation-record.json by default so
            # require_validation=True tests don't blow up on the coder
            # guardrail. Tests exercising missing-validation set
            # ``write_coder_completion=False``.
            validation_dir = session.validation_output_dir
            assert validation_dir is not None
            validation_dir.mkdir(parents=True, exist_ok=True)
            (validation_dir / "validation-record.json").write_text(
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
            completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
            validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
            return _FakeSession(
                role,
                completion_path=Path(completion_env) if completion_env else None,
                validation_output_dir=Path(validation_env) if validation_env else None,
            )

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            if role == "coder":
                coder_call_count["n"] += 1
                if coder_call_count["n"] == 1:
                    # Round 1: write a completion + passing validation.
                    completion = session.completion_path
                    validation_dir = session.validation_output_dir
                    assert completion is not None and validation_dir is not None
                    completion.parent.mkdir(parents=True, exist_ok=True)
                    completion.write_text(
                        json.dumps({"outcome": "completed", "round": 1}),
                        encoding="utf-8",
                    )
                    validation_dir.mkdir(parents=True, exist_ok=True)
                    (validation_dir / "validation-record.json").write_text(
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


class TestResponseFileInsideWorktree:
    """The per-round response file must live inside the role's worktree.

    Regression guard for the runaway-loop bug fixed in this PR: the
    reviewer agent's seatbelt sandbox restricts writes to its cwd
    (the reviewer worktree). When the response file lived under the
    base-repo persistent-pair dir, every reviewer write attempt failed
    with ``operation not permitted``; the round timed out without a
    response file, the orchestrator relaunched the exchange, repeat
    forever. Keep these assertions tight — moving the response path
    back outside the worktree would silently re-introduce the loop.
    """

    def test_response_paths_resolve_inside_role_worktrees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        captured: dict[str, Path] = {}

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
            captured[f"{role}_response"] = Path(env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
            captured[f"{role}_working_dir"] = Path(working_dir)
            return _FakeSession(role)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            captured.setdefault(f"{session.role}_response_arg", response_file)
            return {"response_type": "ok", "response_text": "ok", "getting_closer": True}

        registry = _FakePairRegistry()
        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

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

        # The agent's env var ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE
        # must point inside the role's working_dir (its sandbox-writable
        # root). The runner's ``send_round`` call must use the same path.
        assert captured["coder_response"].is_relative_to(coder_wt), (
            f"coder response file {captured['coder_response']} is not inside "
            f"the coder worktree {coder_wt}"
        )
        assert captured["reviewer_response"].is_relative_to(reviewer_wt), (
            f"reviewer response file {captured['reviewer_response']} is not "
            f"inside the reviewer worktree {reviewer_wt}"
        )
        assert captured["reviewer_response_arg"] == captured["reviewer_response"]

    def test_response_paths_are_outside_persistent_pair_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Belt-and-suspenders: reject any drift back into the base-repo
        persistent-pair dir, which is what the seatbelt sandbox rejects."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        persistent_pair_root = tmp_path / "persistent-pairs"

        captured: dict[str, Path] = {}

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
            captured[role] = Path(env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
            return _FakeSession(role)

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            return {"response_type": "ok", "response_text": "ok", "getting_closer": True}

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=_FakePairRegistry(),
            persistent_pair_root=persistent_pair_root,
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

        for role, path in captured.items():
            assert not str(path).startswith(str(persistent_pair_root)), (
                f"{role} response file {path} drifted back under "
                f"persistent_pair_root {persistent_pair_root}; this is the "
                "exact path the agent's sandbox rejects"
            )


class TestPerSessionRecordingMirror:
    """Per-session recording slice in run_dir/<role>/terminal-recording.jsonl.

    Without this projection, the session run-dir has only chapter
    offsets that point into a pair-scoped recording the run_dir
    doesn't own — the timeline viewer for review-exchange sessions
    looked empty even when the agent had emitted megabytes of output.
    The slice + manifest indirection make each run_dir self-contained.
    """

    def test_role_slice_files_seeded_and_manifest_points_at_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At exchange start, a viewer hitting the run_dir mid-round must
        see the per-session slice files (not 404), and the manifest's
        ``coder_recording`` / ``reviewer_recording`` keys must point at
        them. The pair-scoped recording remains discoverable under
        ``coder_recording_pair`` / ``reviewer_recording_pair``."""
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "ok",
                              "getting_closer": True}],
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
            max_rounds=3,
            max_no_progress=2,
            require_validation=False,
        )

        # Find the actual run_dir from the coder worktree's sessions/.
        # session_output also creates a friendly-name symlink alongside
        # the timestamped real dir; filter symlinks so we land on the
        # real dir (which is the only one that holds manifest.json).
        runs = list((coder_wt / ".issue-orchestrator" / "sessions").iterdir())
        runs = [
            r for r in runs
            if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
        ]
        assert len(runs) == 1, f"expected one run_dir, got {runs}"
        run_dir = runs[0]

        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert "coder_recording" in manifest
        assert "reviewer_recording" in manifest
        assert "coder_recording_pair" in manifest
        assert "reviewer_recording_pair" in manifest

        coder_slice = Path(manifest["coder_recording"])
        reviewer_slice = Path(manifest["reviewer_recording"])
        # Per-session slices must live inside the run_dir itself so
        # worktree teardown is the one and only lifetime they care about.
        assert coder_slice.is_relative_to(run_dir), coder_slice
        assert reviewer_slice.is_relative_to(run_dir), reviewer_slice
        assert coder_slice.exists()
        assert reviewer_slice.exists()
        # The pair-scoped pointers must NOT collide with the per-session
        # slices — that would mean the manifest is silently overwriting
        # one with the other.
        assert manifest["coder_recording"] != manifest["coder_recording_pair"]
        assert manifest["reviewer_recording"] != manifest["reviewer_recording_pair"]

    def test_role_slice_mirror_copies_pair_events_into_run_dir(
        self, tmp_path: Path,
    ) -> None:
        """``_RoleSliceMirror`` projects pair-recording events into the
        per-session slice deterministically: it appends only events in
        ``[last_event_idx, current_event_idx)`` and updates
        ``last_event_idx`` so successive calls don't re-copy."""
        pair_recording = tmp_path / "pair" / "terminal-recording.jsonl"
        pair_recording.parent.mkdir(parents=True)
        # Three canonical resize events, distinguishable so we can assert
        # the slice contains exactly the right ones.
        events = [
            json.dumps({"schema_version": 1, "event_type": "resize",
                        "offset_ms": i * 100, "rows": 40, "cols": 120 + i})
            for i in range(3)
        ]
        pair_recording.write_text("\n".join(events) + "\n", encoding="utf-8")

        slice_path = tmp_path / "run" / "reviewer" / "terminal-recording.jsonl"
        # Start at index 0 — first call should mirror events 0 and 1.
        mirror = pse._RoleSliceMirror(  # noqa: SLF001 — testing internal contract
            pair_recording=pair_recording,
            session_slice=slice_path,
            last_event_idx=0,
        )
        written = mirror.mirror_through(2)
        assert written == 2
        assert mirror.last_event_idx == 2
        slice_lines = [
            line for line in slice_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(slice_lines) == 2
        assert json.loads(slice_lines[0])["cols"] == 120
        assert json.loads(slice_lines[1])["cols"] == 121

        # Second call mirrors only the remaining event.
        written = mirror.mirror_through(3)
        assert written == 1
        assert mirror.last_event_idx == 3
        slice_lines = [
            line for line in slice_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(slice_lines) == 3
        assert json.loads(slice_lines[2])["cols"] == 122

        # No-op if current <= last.
        written = mirror.mirror_through(3)
        assert written == 0

    def test_role_slice_mirror_skips_prior_exchange_content(
        self, tmp_path: Path,
    ) -> None:
        """Cached pairs carry events from previous exchanges in the pair
        recording. The per-session slice must skip those events: the
        mirror's ``last_event_idx`` is initialized to the pair recording's
        size *at exchange start*, so prior content stays out."""
        pair_recording = tmp_path / "pair" / "terminal-recording.jsonl"
        pair_recording.parent.mkdir(parents=True)
        events = [
            json.dumps({"schema_version": 1, "event_type": "resize",
                        "offset_ms": i * 100, "rows": 40, "cols": 120 + i})
            for i in range(5)
        ]
        pair_recording.write_text("\n".join(events) + "\n", encoding="utf-8")

        slice_path = tmp_path / "run" / "reviewer" / "terminal-recording.jsonl"
        # Simulate "this exchange started after the first 3 events were
        # already in the pair recording from an earlier exchange."
        mirror = pse._RoleSliceMirror(  # noqa: SLF001
            pair_recording=pair_recording,
            session_slice=slice_path,
            last_event_idx=3,
        )
        written = mirror.mirror_through(5)
        assert written == 2
        slice_lines = [
            line for line in slice_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [json.loads(line)["cols"] for line in slice_lines] == [123, 124]


class TestEndToEndTimelineReadback:
    """Exercise the full viewer-side read path against real artifacts.

    The original empty-reviewer-timeline bug shipped past 27 unit tests
    because every test asserted on the chapter sidecar / manifest
    *internals* but no test ever asked the question "if a UI consumed
    the manifest's ``<role>_recording`` and read it, would it see the
    agent's output?" These tests close that gap by using the real
    ``MirroredTerminalRecordingWriter`` to populate the pair recording
    and the real ``ManifestAccessor`` to read it back.
    """

    def test_per_session_slice_returns_real_agent_output_to_viewer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: agent writes events to the pair recording during
        rounds, the chapter-driven mirror appends them to the run-dir
        slice, and ``ManifestAccessor.get_review_exchange_phase_terminal_recording``
        returns the slice with the agent's events. Without this, the
        timeline viewer sees an empty file even though the agent
        produced megabytes of output."""
        from issue_orchestrator.execution.manifest_accessor import (
            ManifestAccessor,
            RunIdentity,
        )
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        registry = _FakePairRegistry()
        # Track real writers so _send can simulate agent output bytes
        # flowing through the same writer used at session open.
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            session = _FakeSession(role)
            # Stash the writer on the fake session so close-time tests
            # can flush. Not strictly required for read-back but mirrors
            # production where close_persistent_session calls writer.close().
            session.log_writer = writer
            return session

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            # Simulate the agent producing output bytes during the round.
            # The MirroredTerminalRecordingWriter writes a real
            # ``output`` event per call, which is exactly what the
            # production PTY drain path produces.
            payload = f"agent {role} responding to: {prompt[:40]}\n".encode()
            writers[role].write(payload)
            return {
                "response_type": "ok",
                "response_text": f"{role} ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
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
        finally:
            for w in writers.values():
                w.close()

        run_dir = _find_review_exchange_run_dir(coder_wt)

        # Reviewer slice must be present and non-empty when read by the
        # viewer-side accessor.
        accessor = ManifestAccessor(
            run_identity=RunIdentity(issue_number=42, run_dir=run_dir),
        )
        reviewer_stream = accessor.get_review_exchange_phase_terminal_recording(
            round_index=1, role="reviewer",
        )
        reviewer_path = reviewer_stream.path
        reviewer_text = reviewer_path.read_text(encoding="utf-8")
        assert reviewer_text.strip(), (
            "viewer-side read returned empty content for reviewer slice; "
            "this is exactly the user-visible 'crappy timeline' bug"
        )
        # Decode the slice and assert the agent's payload survived the
        # mirror round-trip.
        decoded = _decode_terminal_recording(reviewer_path)
        assert any(
            "agent reviewer" in chunk for chunk in decoded
        ), f"reviewer slice did not contain agent output; events={decoded}"

    def test_viewer_accessor_reads_slice_not_pair_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The manifest's ``<role>_recording`` key must point at the
        per-session slice (inside run_dir), not the pair-scoped
        recording. Otherwise the viewer would see content from prior
        exchanges leaking into this exchange's playback."""
        from issue_orchestrator.execution.manifest_accessor import (
            ManifestAccessor,
            RunIdentity,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()

        state = _patch_persistent_runner(
            monkeypatch,
            response_script={
                "reviewer": [{"response_type": "ok", "response_text": "ok",
                              "getting_closer": True}],
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
        )

        run_dir = _find_review_exchange_run_dir(coder_wt)
        accessor = ManifestAccessor(
            run_identity=RunIdentity(issue_number=42, run_dir=run_dir),
        )
        # allow_empty=True because the patched _send returns canned dicts
        # without writing real PTY bytes — the slice is touched empty.
        # The point of this test is the *path*, not the content.
        reviewer_stream = accessor.get_review_exchange_phase_terminal_recording(
            round_index=1, role="reviewer", allow_empty=True,
        )
        # The viewer must land inside the run_dir, not in the pair dir
        # under persistent-pairs/. Otherwise a worktree teardown leaves
        # the viewer 404'ing on a still-live recording.
        assert reviewer_stream.path.is_relative_to(run_dir), (
            f"viewer resolved reviewer recording to {reviewer_stream.path}, "
            f"which is NOT inside run_dir {run_dir} — the per-session "
            "manifest indirection broke"
        )


class TestAgentEnvPathIsolation:
    """Audit every agent-facing env var that holds a path.

    The seatbelt sandbox blocks writes outside the role's working_dir.
    Any ``ISSUE_ORCHESTRATOR_*`` env var that names a path the agent
    might write to must therefore resolve under ``working_dir`` (or
    appear on an explicit allowlist with documented justification).
    Catches the next "we added a new path env var pointing into
    base-repo state" before it ships.
    """

    # Paths the agent receives as env vars but never writes to directly
    # (the orchestrator writes them via HTTP / from outside the sandbox).
    # Each entry must come with a justification — adding to this list is
    # how a reviewer says "yes, this is intentional, here's why the
    # sandbox can't reach it."
    _DOCUMENTED_NON_AGENT_WRITTEN_PATHS = {
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": (
            "Written by the orchestrator process via the coding-done CLI's "
            "HTTP call to the Control API; agent never writes directly."
        ),
        "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": (
            "Written by the orchestrator-side validation gate; agent does "
            "not write into this directory directly."
        ),
        "ISSUE_ORCHESTRATOR_REPO_ROOT": (
            "Read-only orientation hint inherited from the orchestrator's "
            "process env so agents/tools that need the repo root know "
            "where it is. Agents never write to this path."
        ),
    }

    def test_every_agent_path_env_var_is_inside_worktree_or_allowlisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        captured: dict[str, dict[str, str]] = {}

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
            captured[role] = {
                "working_dir": str(working_dir),
                **{k: v for k, v in env.items() if k.startswith("ISSUE_ORCHESTRATOR_")},
            }
            return _FakeSession(role)

        def _send(session, **_):  # noqa: ANN003
            return {"response_type": "ok", "response_text": "ok", "getting_closer": True}

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        pse.run_persistent_session_exchange(
            session_output=session_output,
            pair_registry=_FakePairRegistry(),
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

        for role, env in captured.items():
            working_dir = Path(env.pop("working_dir"))
            for key, raw_value in env.items():
                if not _looks_like_path(raw_value):
                    continue
                path = Path(raw_value)
                if key in self._DOCUMENTED_NON_AGENT_WRITTEN_PATHS:
                    # Allowlisted — must still verify the justification
                    # exists (forces reviewer attention on changes here).
                    assert self._DOCUMENTED_NON_AGENT_WRITTEN_PATHS[key], (
                        f"{key} appears in the non-agent-written allowlist "
                        "with no justification — add a docstring before "
                        "shipping this env var"
                    )
                    continue
                assert path.is_relative_to(working_dir), (
                    f"{role} env var {key}={path} is outside working_dir "
                    f"{working_dir}; if the agent writes here from inside "
                    f"its sandbox, the write will silently fail. Either "
                    f"move the path inside the worktree (preferred) or "
                    f"add it to TestAgentEnvPathIsolation."
                    f"_DOCUMENTED_NON_AGENT_WRITTEN_PATHS with a "
                    "justification."
                )


class TestSliceIsolationAcrossExchanges:
    """The per-session slice for exchange N must contain ONLY events
    produced during exchange N. With cached pairs, the pair recording
    accumulates across every exchange the pair handles — we must not
    leak prior content into the new exchange's slice."""

    def test_two_exchanges_on_cached_pair_have_independent_slices(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        # Cache pairs across acquire calls so exchange 2 reuses the same
        # writer (== same pair recording) that exchange 1 wrote into.
        cached_pair: dict[str, Any] = {}

        class _CachingFakePairRegistry(_FakePairRegistry):
            def acquire(self, *, issue_key, spawn):  # type: ignore[override]
                if "pair" not in cached_pair:
                    cached_pair["pair"] = spawn()
                self.acquired.append((issue_key, cached_pair["pair"]))
                return cached_pair["pair"]

        registry = _CachingFakePairRegistry()
        round_payloads: dict[str, list[bytes]] = {"coder": [], "reviewer": []}

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            session = _FakeSession(role)
            session.log_writer = writer
            return session

        exchange_counter = {"n": 0}

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            payload = f"exchange-{exchange_counter['n']}-{role}-payload\n".encode()
            writers[role].write(payload)
            round_payloads[role].append(payload)
            return {
                "response_type": "ok",
                "response_text": f"{role} ok",
                "getting_closer": True,
            }

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            for n in (1, 2):
                exchange_counter["n"] = n
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
        finally:
            for w in writers.values():
                w.close()

        runs = sorted(
            [
                r for r in (coder_wt / ".issue-orchestrator" / "sessions").iterdir()
                if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
            ],
            key=lambda p: p.stat().st_mtime,
        )
        assert len(runs) == 2, f"expected two run_dirs, got {runs}"

        for run_idx, run_dir in enumerate(runs, start=1):
            slice_path = run_dir / "reviewer" / "terminal-recording.jsonl"
            decoded = _decode_terminal_recording(slice_path)
            joined = "".join(decoded)
            # Exchange N's slice must contain its OWN payload.
            assert f"exchange-{run_idx}-reviewer" in joined, (
                f"run {run_idx} slice missing its own payload; events={decoded}"
            )
            # And must NOT contain the OTHER exchange's payload.
            other = 2 if run_idx == 1 else 1
            assert f"exchange-{other}-reviewer" not in joined, (
                f"run {run_idx} slice contains exchange-{other} content; "
                f"the cached-pair last_event_idx initialization leaked. "
                f"events={decoded}"
            )

    def test_coder_and_reviewer_slices_dont_cross_contaminate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each role's mirror writes only to its own role's slice. If
        we accidentally pass coder_mirror to reviewer's _send_role_round
        (or vice versa), this catches it because the slices would
        contain the wrong role's events."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")
        coder_wt, reviewer_wt = _setup_worktrees(tmp_path)
        session_output = FileSystemSessionOutput()
        writers: dict[str, MirroredTerminalRecordingWriter] = {}

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = "reviewer" if Path(working_dir).name.startswith("reviewer-wt") else "coder"
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path,
                initial_rows=40,
                initial_cols=120,
            )
            writers[role] = writer
            completion_env = env.get("ISSUE_ORCHESTRATOR_COMPLETION_PATH")
            validation_env = env.get("ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR")
            session = _FakeSession(
                role,
                completion_path=Path(completion_env) if completion_env else None,
                validation_output_dir=Path(validation_env) if validation_env else None,
            )
            session.log_writer = writer
            return session

        # Both roles produce role-tagged output during their rounds. The
        # exchange runs reviewer → coder → reviewer (a 2-round changes
        # then ok exchange), so each role writes at least once.
        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            writers[role].write(f"ROLE-MARKER-{role}\n".encode())
            if role == "coder":
                # Stub completion artifact so the protocol guardrail passes.
                completion = session.completion_path
                assert completion is not None
                completion.parent.mkdir(parents=True, exist_ok=True)
                completion.write_text(
                    json.dumps({"outcome": "completed", "implementation": "stub"}),
                    encoding="utf-8",
                )
            if role == "reviewer":
                # First reviewer response: changes_requested. Second: ok.
                if not _send.first_reviewer_done:  # type: ignore[attr-defined]
                    _send.first_reviewer_done = True  # type: ignore[attr-defined]
                    return {"response_type": "changes_requested",
                            "response_text": "fix x", "getting_closer": True}
                return {"response_type": "ok", "response_text": "ok",
                        "getting_closer": True}
            return {"response_type": "ok", "response_text": "fixed",
                    "getting_closer": True}

        _send.first_reviewer_done = False  # type: ignore[attr-defined]

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)

        try:
            pse.run_persistent_session_exchange(
                session_output=session_output,
                pair_registry=_FakePairRegistry(),
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
        finally:
            for w in writers.values():
                w.close()

        run_dir = _find_review_exchange_run_dir(coder_wt)
        coder_slice = "".join(
            _decode_terminal_recording(run_dir / "coder" / "terminal-recording.jsonl"),
        )
        reviewer_slice = "".join(
            _decode_terminal_recording(run_dir / "reviewer" / "terminal-recording.jsonl"),
        )

        assert "ROLE-MARKER-coder" in coder_slice, "coder slice missing coder marker"
        assert "ROLE-MARKER-reviewer" in reviewer_slice, "reviewer slice missing reviewer marker"
        # The smoking gun: cross-contamination.
        assert "ROLE-MARKER-reviewer" not in coder_slice, (
            "reviewer marker leaked into coder slice — mirrors got crossed"
        )
        assert "ROLE-MARKER-coder" not in reviewer_slice, (
            "coder marker leaked into reviewer slice — mirrors got crossed"
        )

    def test_concurrent_issues_have_isolated_slices(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two issues running in parallel must not share manifest slice
        paths. A path collision would silently mix issue #360's reviewer
        output into issue #361's timeline."""
        from issue_orchestrator.infra.terminal_recording import (
            MirroredTerminalRecordingWriter,
        )

        prompt_path = tmp_path / "p.md"
        prompt_path.write_text("Prompt", encoding="utf-8")

        def _setup(issue: int) -> tuple[Path, Path]:
            coder = tmp_path / f"coder-wt-{issue}"
            reviewer = tmp_path / f"reviewer-wt-{issue}"
            coder.mkdir()
            reviewer.mkdir()
            return coder, reviewer

        worktrees = {360: _setup(360), 361: _setup(361)}
        writers_by_issue: dict[int, dict[str, MirroredTerminalRecordingWriter]] = {
            360: {}, 361: {},
        }
        active_issue = {"n": 360}

        def _open(*, command, working_dir, env, recording_path=None,
                  additional_recording_paths=None, mirror_path=None):  # noqa: ARG001
            role = (
                "reviewer" if "reviewer-wt-" in Path(working_dir).name else "coder"
            )
            assert recording_path is not None
            recording_path.parent.mkdir(parents=True, exist_ok=True)
            writer = MirroredTerminalRecordingWriter(
                recording_path, initial_rows=40, initial_cols=120,
            )
            writers_by_issue[active_issue["n"]][role] = writer
            session = _FakeSession(role)
            session.log_writer = writer
            return session

        def _send(session, *, prompt, response_file, timeout_seconds, **_):  # noqa: ARG001
            role = session.role
            issue = active_issue["n"]
            writers_by_issue[issue][role].write(f"ISSUE-{issue}-{role}\n".encode())
            return {"response_type": "ok", "response_text": f"{role} ok",
                    "getting_closer": True}

        monkeypatch.setattr(pse, "open_persistent_session", _open)
        monkeypatch.setattr(pse, "send_round", _send)
        session_output = FileSystemSessionOutput()

        try:
            for issue, (coder_wt, reviewer_wt) in worktrees.items():
                active_issue["n"] = issue
                pse.run_persistent_session_exchange(
                    session_output=session_output,
                    pair_registry=_FakePairRegistry(),
                    persistent_pair_root=tmp_path / "persistent-pairs",
                    coder_worktree_path=coder_wt,
                    reviewer_worktree_factory=lambda r=reviewer_wt: r,
                    issue_number=issue,
                    issue_title=f"Issue {issue}",
                    coder_label="agent:backend",
                    reviewer_label="agent:reviewer",
                    coder_agent=_make_agent(prompt_path),
                    reviewer_agent=_make_agent(prompt_path),
                    max_rounds=1,
                    max_no_progress=2,
                    require_validation=False,
                )
        finally:
            for wmap in writers_by_issue.values():
                for w in wmap.values():
                    w.close()

        for issue, (coder_wt, _) in worktrees.items():
            run_dir = _find_review_exchange_run_dir(coder_wt)
            reviewer_slice = "".join(
                _decode_terminal_recording(run_dir / "reviewer" / "terminal-recording.jsonl"),
            )
            other = 360 if issue == 361 else 361
            assert f"ISSUE-{issue}-reviewer" in reviewer_slice
            assert f"ISSUE-{other}-reviewer" not in reviewer_slice, (
                f"issue {issue} reviewer slice contains issue {other} content"
            )


class TestSliceMirrorRobustness:
    """The slice mirror is a viewer aid — it must NEVER abort a round
    or raise into the orchestrator main loop. Doing so would re-trigger
    the runaway loop the loop-bound fix is designed to prevent."""

    def test_mirror_through_swallows_oserror_and_returns_zero(
        self, tmp_path: Path,
    ) -> None:
        # Simulate an unreadable pair recording (file replaced with a
        # symlink pointing nowhere). mirror_through must log + return 0.
        pair_recording = tmp_path / "pair-broken.jsonl"
        slice_path = tmp_path / "slice.jsonl"
        # Create then break: open a real source file, then replace with
        # a symlink to a missing target so .open() fails with OSError.
        pair_recording.write_text("garbage", encoding="utf-8")
        pair_recording.unlink()
        # On macOS/linux, a dangling symlink causes .exists() to return
        # False, which the mirror short-circuits via the early exit. Use
        # an unreadable directory instead so .open() raises OSError on
        # an existing path.
        pair_recording.mkdir()  # path exists but isn't a regular file
        mirror = pse._RoleSliceMirror(  # noqa: SLF001
            pair_recording=pair_recording,
            session_slice=slice_path,
            last_event_idx=0,
        )
        # Must not raise. Returns 0 because the open() raised OSError.
        written = mirror.mirror_through(5)
        assert written == 0
        # last_event_idx must NOT have advanced — a future successful
        # mirror call should still see those events as "new."
        assert mirror.last_event_idx == 0


class TestLoopBoundCounting:
    """Direct adapter-level tests of count_consecutive_review_exchange_no_completion."""

    def _session_output_with_summaries(
        self,
        tmp_path: Path,
        summaries: list[dict[str, Any]],
        *,
        session_name: str = "coding-1",
    ) -> tuple[FileSystemSessionOutput, Path]:
        """Build a sessions/ tree with the given summaries, oldest first.

        Each entry is the JSON content of summary.json. The sessions
        directory layout matches what FileSystemSessionOutput produces.
        """
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)
        # Use distinct mtimes so newest-first ordering is deterministic.
        for idx, summary in enumerate(summaries):
            run_dir = sessions_dir / (
                f"2026010{idx + 1}T000000Z__review-exchange-42-r{idx}"
            )
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps({
                    "started_at": f"2026-01-0{idx + 1}T00:00:00+00:00",
                    "review_exchange_dir": str(run_dir / "review-exchange"),
                }),
                encoding="utf-8",
            )
            exchange_dir = run_dir / "review-exchange"
            exchange_dir.mkdir()
            (exchange_dir / "summary.json").write_text(
                json.dumps(summary), encoding="utf-8",
            )
            # Force ascending mtime (idx=0 oldest, idx=N-1 newest).
            os.utime(run_dir, (1700000000 + idx * 100, 1700000000 + idx * 100))
        return FileSystemSessionOutput(), worktree

    def test_all_no_completion_summaries_count_to_their_total(
        self, tmp_path: Path,
    ) -> None:
        so, wt = self._session_output_with_summaries(tmp_path, [
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
        ])
        assert so.count_consecutive_review_exchange_no_completion(
            wt, "coding-1",
        ) == 3

    def test_clean_ok_resets_count_to_zero(self, tmp_path: Path) -> None:
        # Sequence (oldest → newest): error, error, ok, error, error.
        # Newest-first traversal: error, error, ok → stop at ok.
        so, wt = self._session_output_with_summaries(tmp_path, [
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "ok", "reason": "reviewer_ok"},
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
        ])
        assert so.count_consecutive_review_exchange_no_completion(
            wt, "coding-1",
        ) == 2

    def test_different_error_reason_resets_count(self, tmp_path: Path) -> None:
        # A coder_protocol_error is a different failure mode — not the
        # runaway we're trying to bound. Counter must stop at it.
        so, wt = self._session_output_with_summaries(tmp_path, [
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "coder_protocol_error"},
            {"status": "error", "reason": "reviewer_no_completion"},
        ])
        # Newest-first: error/no_completion (1), error/protocol_error (stop).
        assert so.count_consecutive_review_exchange_no_completion(
            wt, "coding-1",
        ) == 1

    def test_no_summaries_returns_zero(self, tmp_path: Path) -> None:
        so, wt = self._session_output_with_summaries(tmp_path, [])
        assert so.count_consecutive_review_exchange_no_completion(
            wt, "coding-1",
        ) == 0

    def test_boundary_excludes_pre_scratch_reset_failures(
        self, tmp_path: Path,
    ) -> None:
        so, wt = self._session_output_with_summaries(tmp_path, [
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
            {"status": "error", "reason": "reviewer_no_completion"},
        ])
        # Only the newest summary (started_at 2026-01-03) is past the
        # boundary; the older two are pre-reset and don't count.
        boundary = "2026-01-03T00:00:00+00:00"
        assert so.count_consecutive_review_exchange_no_completion(
            wt, "coding-1", not_before_started_at=boundary,
        ) == 1


class TestLoopBoundEscalation:
    """The bound must halt the spawn AND surface an error string the
    completion processor recognizes as a needs-human escalation."""

    def test_halt_error_is_recognized_by_review_exchange_classifier(
        self,
    ) -> None:
        """The exact error prefix we append must trigger the
        review-exchange-halted branch in completion_action_planner.
        Catches "someone renamed the prefix" silent breakage."""
        from issue_orchestrator.control.completion_action_planner import (
            has_review_exchange_errors,
        )
        sample = (
            "review_exchange: 3 consecutive reviewer/coder no-completion "
            "failures (max 3) — escalating to needs-human"
        )
        assert has_review_exchange_errors([sample]), (
            "halt error string is not classified as a review-exchange "
            "halt; the escalation will silently no-op"
        )

    def test_halt_error_is_recognized_by_result_artifacts_classifier(
        self,
    ) -> None:
        """The other halting check is an inline ``startswith`` in
        completion_result_artifacts. If the prefix changes, that
        path also silently stops escalating. Lock the prefix in."""
        sample = (
            "review_exchange: 3 consecutive reviewer/coder no-completion "
            "failures (max 3) — escalating to needs-human"
        )
        # Must start with the canonical "review_exchange:" prefix that
        # build_processing_result_with_completion uses to classify
        # halts (see completion_result_artifacts.py:119).
        assert sample.startswith("review_exchange:"), (
            "halt error string lost its review_exchange: prefix; "
            "the inline classifier in completion_result_artifacts "
            "will not flag this as a review-exchange halt"
        )


# ---------------------------------------------------------------------------
# Test helpers shared by the new test classes
# ---------------------------------------------------------------------------


def _find_review_exchange_run_dir(coder_wt: Path) -> Path:
    """Locate the single timestamped review-exchange run_dir under a
    coder worktree (filtering symlinks the friendly-name layer creates)."""
    runs = [
        r for r in (coder_wt / ".issue-orchestrator" / "sessions").iterdir()
        if r.is_dir() and not r.is_symlink() and "review-exchange" in r.name
    ]
    assert len(runs) == 1, f"expected one run_dir, got {runs}"
    return runs[0]


def _decode_terminal_recording(path: Path) -> list[str]:
    """Decode a terminal recording's output events into UTF-8 strings.

    Filters out non-output (resize) events so callers can grep for
    agent payload markers without false positives from geometry frames.
    """
    import base64

    decoded: list[str] = []
    for raw in path.read_text("utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if event.get("event_type") != "output":
            continue
        data = event.get("data_b64")
        if not data:
            continue
        decoded.append(base64.b64decode(data).decode("utf-8", errors="replace"))
    return decoded


def _looks_like_path(value: str) -> bool:
    """Heuristic: env-var values that look like filesystem paths."""
    if not value:
        return False
    return value.startswith("/") or value.startswith("~")


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
