"""Tests for the session-recording viewer's render-mode dispatch.

The backend chooses how the UI should present a ``terminal-recording.jsonl``:

- ``render_mode="terminal"`` for Claude TUI and raw PTY: the JS replay viewer
  feeds events into an xterm emulator (existing behaviour).
- ``render_mode="transcript"`` for Codex ``exec --json`` streams: the PTY
  bytes are a JSON event stream; the emulator would render envelope JSON as
  garbled text, so the backend pre-decodes through the PR C prettifier.

These tests drive the dispatcher directly so they're fast and provider-
format-realistic. The codex side uses the real captured reviewer log as
fixture (same approach as ``test_session_log_prettify``).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from issue_orchestrator.entrypoints.web_session_routes import (
    _render_mode_for_recording,
)


FIXTURES = Path(__file__).parent.parent / "fixtures" / "session_logs"


def _codex_events_from_fixture() -> list[dict[str, object]]:
    """Wrap the real codex reviewer log as output-event records.

    A PTY recording captures whatever bytes the subprocess wrote to stdout.
    Codex writes newline-delimited JSON, so the recording contains those
    bytes in ``output`` events. We recreate that shape from the captured
    ``agent-output.log`` so the dispatcher sees exactly what it would see
    in production.
    """
    raw = (FIXTURES / "codex_reviewer.log").read_text()
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return [
        {
            "event_type": "output",
            "offset_ms": 0,
            "schema_version": 1,
            "data_b64": encoded,
        }
    ]


def _claude_tui_events() -> list[dict[str, object]]:
    """Construct a Claude-TUI-like PTY recording.

    Claude's TUI writes ANSI escape sequences and unicode box-drawing
    characters. The dispatcher must keep those on the terminal-emulator path
    rather than trying to prettify them.
    """
    raw = (
        "\x1b[?25l\x1b[?2004h\x1b[1mClaude\x1b[22m Code v2.1.112\r\n"
        "\x1b[38;2;215;119;87m ▐\x1b[48;2;0;0;0m▛███▜\x1b[49m▌\r\n"
        "> read the task file\r\n"
    )
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return [
        {
            "event_type": "output",
            "offset_ms": 0,
            "schema_version": 1,
            "data_b64": encoded,
        }
    ]


def test_codex_recording_dispatches_to_transcript_mode() -> None:
    events = _codex_events_from_fixture()

    mode, transcript = _render_mode_for_recording(events)

    assert mode == "transcript"
    assert transcript is not None and transcript, "codex dispatch must produce transcript lines"
    joined = "\n".join(transcript)
    # Prose from the fixture survives intact.
    assert "I’ll read the round-specific reviewer prompt first" in joined
    # Commands render as shell prompts (not JSON envelopes).
    assert "$ /bin/zsh -lc 'git status --short'" in joined
    # No envelope leakage.
    envelope_lines = [
        line for line in transcript if line.startswith('{"type":"item.')
    ]
    assert envelope_lines == []


def test_claude_tui_recording_stays_on_terminal_mode() -> None:
    events = _claude_tui_events()

    mode, transcript = _render_mode_for_recording(events)

    assert mode == "terminal"
    assert transcript is None


def test_plain_log_with_stray_codex_line_stays_on_terminal_mode() -> None:
    """A non-codex file with a single codex-shaped line must not hijack render mode.

    Mirrors the PR-C structural-commit rule: only the FIRST non-blank decoded
    line decides. A stray ``{"type":"thread.started"}`` in the middle of
    plain text is almost certainly not a codex recording.
    """
    raw = (
        "building\n"
        "running tests\n"
        '{"type": "thread.started"}\n'
        "done\n"
    )
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    events = [
        {
            "event_type": "output",
            "offset_ms": 0,
            "schema_version": 1,
            "data_b64": encoded,
        }
    ]

    mode, transcript = _render_mode_for_recording(events)

    assert mode == "terminal"
    assert transcript is None


def test_empty_recording_stays_on_terminal_mode() -> None:
    # Empty recording should default to the emulator path — the UI handles
    # "no content" gracefully there. Switching to transcript with an empty
    # list would be a misleading user-facing state change.
    assert _render_mode_for_recording([]) == ("terminal", None)


def test_non_output_events_do_not_fool_the_sniffer() -> None:
    # Geometry / signal events shouldn't count as decodable output. Only
    # actual ``output`` events participate.
    events = [
        {"event_type": "geometry", "rows": 40, "cols": 120, "offset_ms": 0},
        {"event_type": "signal", "signal": "SIGWINCH", "offset_ms": 10},
    ]
    assert _render_mode_for_recording(events) == ("terminal", None)


def test_codex_partial_chunk_still_triggers_transcript_mode() -> None:
    """Realism: codex often writes one JSON line per chunk. Multiple chunks
    should reassemble into a single transcript.
    """
    chunks = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n",
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "agent_message", "text": "Ready."},
            }
        ) + "\n",
    ]
    events = [
        {
            "event_type": "output",
            "offset_ms": idx,
            "schema_version": 1,
            "data_b64": base64.b64encode(chunk.encode()).decode("ascii"),
        }
        for idx, chunk in enumerate(chunks)
    ]

    mode, transcript = _render_mode_for_recording(events)

    assert mode == "transcript"
    assert transcript == ["Ready."]
