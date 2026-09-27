"""What an agent's terminal showed last, from its recording.

An agent parked on a startup dialog emits no event: the tech-lead exam's
first Case B run sat 35 minutes on Claude Code's "Quick safety check" with
"No, exit" highlighted, and the engine logged nothing. The session's own
``terminal-recording.jsonl`` still shows it, so the exam renders that
recording's final screen with the engine's viewport and normalizer — the
same ones the interaction rules match against — and reports it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Iterable

from ...execution.session_interactions import normalize_terminal_text
from ...infra.terminal_viewport import TerminalViewport

#: A session silent this long is waiting on something, not thinking.
SILENT_SCREEN_SECONDS = 120.0
#: How much of a silent screen the report quotes.
SCREEN_QUOTE_CHARS = 240


@dataclass(frozen=True)
class RecordedScreen:
    text: str
    """The final screen's written rows, normalized like the interaction rules see it."""
    last_output_offset_ms: int


def render_recording(lines: Iterable[str]) -> RecordedScreen:
    """Replay a ``terminal-recording.jsonl`` into its final screen."""
    viewport: TerminalViewport | None = None
    last_offset = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event.get("event_type")
        if kind == "resize":
            viewport = TerminalViewport(rows=int(event["rows"]), cols=int(event["cols"]))
        elif kind == "output":
            if viewport is None:
                raise ValueError(f"recording line {number}: output before any resize")
            viewport.feed(base64.b64decode(event["data_b64"]))
            last_offset = int(event["offset_ms"])
    if viewport is None:
        raise ValueError("recording has no resize event; it never sized a terminal")
    return RecordedScreen(
        text=normalize_terminal_text(" ".join(viewport.render().written_rows)),
        last_output_offset_ms=last_offset,
    )


def silent_screen(session: str, screen: RecordedScreen, *, silent_seconds: float) -> str:
    """The stall line for a session parked on a screen, or ``""`` if it is not."""
    if silent_seconds < SILENT_SCREEN_SECONDS or not screen.text:
        return ""
    quote = screen.text[-SCREEN_QUOTE_CHARS:]
    return f"{session} silent {int(silent_seconds)}s on screen: {quote!r}"
