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

    dispatch = _render_mode_for_recording(events)

    assert dispatch.mode == "transcript"
    assert dispatch.transcript_lines is not None and dispatch.transcript_lines
    assert dispatch.transcript_hash is not None
    assert len(dispatch.transcript_hash) == 64  # sha256 hex
    joined = "\n".join(dispatch.transcript_lines)
    # Prose from the fixture survives intact.
    assert "I’ll read the round-specific reviewer prompt first" in joined
    # Commands render as shell prompts (not JSON envelopes).
    assert "$ /bin/zsh -lc 'git status --short'" in joined
    # No envelope leakage.
    envelope_lines = [
        line for line in dispatch.transcript_lines if line.startswith('{"type":"item.')
    ]
    assert envelope_lines == []


def test_transcript_hash_is_stable_across_calls() -> None:
    """Same input → same hash, so the frontend's since_hash short-circuit works."""
    events = _codex_events_from_fixture()

    first = _render_mode_for_recording(events)
    second = _render_mode_for_recording(events)

    assert first.transcript_hash == second.transcript_hash
    assert first.transcript_lines == second.transcript_lines


def test_transcript_hash_changes_when_recording_grows() -> None:
    """Appending new codex events must change the hash (drives incremental refresh)."""
    events = _codex_events_from_fixture()
    baseline = _render_mode_for_recording(events)

    extra_chunk = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "new-1", "type": "agent_message", "text": "Later note."},
        }
    ) + "\n"
    events_plus_one = events + [
        {
            "event_type": "output",
            "offset_ms": 9999,
            "schema_version": 1,
            "data_b64": base64.b64encode(extra_chunk.encode()).decode("ascii"),
        }
    ]

    grown = _render_mode_for_recording(events_plus_one)

    assert grown.transcript_hash != baseline.transcript_hash
    assert grown.transcript_lines is not None
    assert "Later note." in "\n".join(grown.transcript_lines)


def test_claude_tui_recording_stays_on_terminal_mode() -> None:
    events = _claude_tui_events()

    dispatch = _render_mode_for_recording(events)

    assert dispatch.mode == "terminal"
    assert dispatch.transcript_lines is None
    assert dispatch.transcript_hash is None


def test_plain_log_with_stray_codex_line_stays_on_terminal_mode() -> None:
    """A non-codex file with a single codex-shaped line must not hijack render mode.

    Mirrors the PR-C structural-commit rule: only the FIRST complete decoded
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

    assert _render_mode_for_recording(events).mode == "terminal"


def test_empty_recording_stays_on_terminal_mode() -> None:
    # Empty recording should default to the emulator path — the UI handles
    # "no content" gracefully there. Switching to transcript with an empty
    # list would be a misleading user-facing state change.
    dispatch = _render_mode_for_recording([])
    assert dispatch.mode == "terminal"
    assert dispatch.transcript_lines is None


def test_non_output_events_do_not_fool_the_sniffer() -> None:
    # Geometry / signal events shouldn't count as decodable output. Only
    # actual ``output`` events participate.
    events = [
        {"event_type": "geometry", "rows": 40, "cols": 120, "offset_ms": 0},
        {"event_type": "signal", "signal": "SIGWINCH", "offset_ms": 10},
    ]
    assert _render_mode_for_recording(events).mode == "terminal"


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

    dispatch = _render_mode_for_recording(events)

    assert dispatch.mode == "transcript"
    assert dispatch.transcript_lines == ["Ready."]


def test_large_first_codex_event_still_classified_correctly() -> None:
    """A first ``agent_message`` whose text exceeds the old 4K char preview
    must still be recognised as codex. Reviewer prose blocks regularly
    pass that bound and the old char-count sniff would straddle the
    boundary mid-JSON-line and mis-classify.
    """
    big_text = "very long reviewer prose " * 300  # ~8K characters
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "big", "type": "agent_message", "text": big_text},
        }
    ) + "\n"
    events = [
        {
            "event_type": "output",
            "offset_ms": 0,
            "schema_version": 1,
            "data_b64": base64.b64encode(line.encode()).decode("ascii"),
        }
    ]

    dispatch = _render_mode_for_recording(events)

    assert dispatch.mode == "transcript"
    assert dispatch.transcript_lines is not None
    assert big_text.strip() in "\n".join(dispatch.transcript_lines)


def test_codex_stream_without_terminating_newline_still_detected() -> None:
    """Real PTY captures don't always end on ``\\n``; the sniffer must still
    classify a complete codex JSON line that lacks a trailing newline.
    """
    line = json.dumps({"type": "thread.started", "thread_id": "t1"})
    events = [
        {
            "event_type": "output",
            "offset_ms": 0,
            "schema_version": 1,
            "data_b64": base64.b64encode(line.encode()).decode("ascii"),
        }
    ]

    dispatch = _render_mode_for_recording(events)

    assert dispatch.mode == "transcript"
