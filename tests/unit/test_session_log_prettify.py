"""Tests for :mod:`session_log_prettify` using real captured agent output.

The fixtures under ``tests/fixtures/session_logs/`` come from a live
review-exchange on tixmeup#230 — they are the exact bytes the orchestrator
captured from the reviewer/coder subprocesses. Asserting against real data
keeps the per-provider extractors honest when either side changes format.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.session_log_prettify import (
    extract_claude_transcript,
    extract_codex_transcript,
    prettify_session_log,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "session_logs"


def _read(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


# ---------------------------------------------------------------------------
# Codex extractor — using the live reviewer log from tixmeup #230
# ---------------------------------------------------------------------------


def test_codex_extractor_renders_real_reviewer_log_as_transcript() -> None:
    transcript = extract_codex_transcript(_read("codex_reviewer.log"))

    assert transcript is not None
    joined = "\n".join(transcript)

    # The reviewer's opening narration must appear verbatim.
    assert (
        "I’ll read the round-specific reviewer prompt first" in joined
    ), "codex agent_message text must be preserved"
    # Commands are rendered as shell prompts so the reader sees exactly what ran.
    assert "$ /bin/zsh -lc 'git status --short'" in joined
    assert "$ /bin/zsh -lc 'git branch --show-current'" in joined
    # Aggregated output must follow the command that produced it.
    command_idx = transcript.index("$ /bin/zsh -lc 'git status --short'")
    tail = "\n".join(transcript[command_idx : command_idx + 10])
    assert ".issue-orchestrator/sessions/" in tail
    # The envelope itself (`{"type":"item.started",...}` on its own line) must
    # never reach the transcript — but fragments of JSON in captured command
    # output legitimately can, so we only reject raw envelope lines.
    envelope_lines = [
        line
        for line in transcript
        if line.startswith('{"type":"item.')
        or line.startswith('{"type":"thread.')
        or line.startswith('{"type":"turn.')
    ]
    assert envelope_lines == [], envelope_lines


def test_codex_extractor_collapses_started_and_completed_to_final_state() -> None:
    # A single command produces two events: item.started then item.completed
    # (with aggregated_output). We must see the command + output once, not
    # the command twice.
    events = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "echo hi",
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "echo hi",
                    "aggregated_output": "hi\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        ),
    ]
    transcript = extract_codex_transcript(events)

    assert transcript is not None
    assert transcript.count("$ echo hi") == 1
    assert "hi" in transcript


def test_codex_extractor_marks_nonzero_exits() -> None:
    events = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "c1",
                    "type": "command_execution",
                    "command": "false",
                    "aggregated_output": "",
                    "exit_code": 7,
                    "status": "completed",
                },
            }
        )
    ]
    transcript = extract_codex_transcript(events)

    assert transcript is not None
    assert any("exit code: 7" in line for line in transcript)


def test_codex_extractor_returns_none_for_non_codex_input() -> None:
    # Raw ANSI terminal output is not Codex JSON — extractor must decline
    # so the dispatcher can fall through to another strategy.
    assert extract_codex_transcript(["\x1b[32mhello\x1b[0m", "plain text"]) is None
    assert extract_codex_transcript([""]) is None


def test_codex_extractor_emits_breadcrumb_for_pure_meta_streams() -> None:
    # A stream with only thread.started/turn.started but no items is still
    # recognisably codex; we emit a single-line breadcrumb so the UI doesn't
    # render a confusing blank panel with no explanation.
    events = [
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "turn.started"}),
    ]
    result = extract_codex_transcript(events)
    assert result == ["(codex session produced no items)"]


def test_codex_extractor_rejects_log_with_codex_line_in_the_middle() -> None:
    """Plain log + one stray codex-shaped line: the unrelated lines must survive.

    Codex exec logs always OPEN with a codex event. A file that starts with
    plain text and happens to contain a codex-looking JSON record later is
    almost certainly something else (a mixed log, a pasted fragment, an
    aggregator). Committing to the codex path would drop every non-codex
    line — the regression the reviewer flagged. Rejecting lets the
    dispatcher fall through to terminal-cleaning, which preserves the
    plain-text content.
    """
    mixed = [
        "plain unrelated log line",
        "another unrelated line",
        '{"type": "thread.started"}',  # stray fragment, NOT a codex log
        "more unrelated content",
    ]
    assert extract_codex_transcript(mixed) is None


def test_codex_extractor_rejects_non_codex_json_prelude() -> None:
    """First-line-must-be-codex rule also rejects non-codex JSON streams."""
    claude_like = [
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "thread.started"}),
    ]
    assert extract_codex_transcript(claude_like) is None


def test_codex_extractor_tolerates_leading_blank_lines() -> None:
    """Whitespace-only prelude is skipped; the first real line is the gate."""
    events = [
        "",
        "   ",
        json.dumps({"type": "thread.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "a1", "type": "agent_message", "text": "hello"},
            }
        ),
    ]
    transcript = extract_codex_transcript(events)
    assert transcript is not None
    assert "hello" in transcript


def test_prettify_falls_through_to_terminal_cleaning_for_mixed_file() -> None:
    """End-to-end: mixed file with stray codex line renders plain lines, not breadcrumb."""
    mixed = [
        "\x1b[32mbuilding\x1b[0m",
        "running tests",
        '{"type": "thread.started"}',
        "\x1b[31mfailed\x1b[0m",
    ]
    transcript = prettify_session_log(mixed)
    joined = "\n".join(transcript)
    # ANSI stripped, plain text preserved.
    assert "building" in joined
    assert "running tests" in joined
    assert "failed" in joined
    # Did NOT commit to codex — no breadcrumb.
    assert "(codex session produced no items)" not in joined
    assert "\x1b[" not in joined


# ---------------------------------------------------------------------------
# Claude extractor — unchanged behaviour, but exposed under the new name.
# ---------------------------------------------------------------------------


def test_claude_extractor_still_recognises_stream_json() -> None:
    events = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                },
            }
        ),
        json.dumps({"type": "result", "result": "ignored — assistant already captured"}),
    ]

    transcript = extract_claude_transcript(events)

    assert transcript == ["hello"]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_prettify_dispatches_codex_format() -> None:
    transcript = prettify_session_log(_read("codex_reviewer.log"))

    # If dispatch went to Codex, we see rendered shell prompts. If it fell
    # back to raw lines, we would see the envelope on every line.
    joined = "\n".join(transcript)
    assert "$ /bin/zsh -lc 'git status --short'" in joined
    envelope_lines = [
        line for line in transcript if line.startswith('{"type":"item.')
    ]
    assert envelope_lines == []


def test_prettify_falls_back_to_terminal_cleaning_for_unknown_format() -> None:
    # Raw ANSI output (Claude Code TUI) isn't JSON — dispatcher must clean it.
    raw = ["\x1b[32mHello, world\x1b[0m", "plain text"]
    transcript = prettify_session_log(raw)

    joined = "\n".join(transcript)
    assert "\x1b[" not in joined  # ANSI stripped
    assert "Hello, world" in joined
    assert "plain text" in joined


def test_prettify_preserves_claude_stream_json() -> None:
    events = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "why hello\nthere"}],
                },
            }
        )
    ]
    assert prettify_session_log(events) == ["why hello", "there"]
