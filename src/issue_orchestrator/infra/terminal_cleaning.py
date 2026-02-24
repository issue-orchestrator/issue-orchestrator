"""Terminal output cleaning utilities.

Provides functions for stripping ANSI escape sequences, filtering spinner
animations, deduplicating consecutive lines, and decoding Claude stream-json
format.  Also provides ``CleaningLogWriter``, a file-like wrapper that
implements the pexpect ``logfile`` interface and writes cleaned text in
real time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# ANSI / control-character patterns
# ---------------------------------------------------------------------------

# Matches ANSI escape sequences and related control characters:
# - \x1b[...m  (SGR – colors, bold, etc.)
# - \x1b[...A/B/C/D  (cursor movement)
# - \x1b]...BEL  (OSC – terminal titles)
# - \x1b[?...h/l/s/u  (private mode set/reset like ?2026h)
# - \x1b[>...u/c  (extended key sequences)
# - \x1b[<u  (pop key mode)
# - \x1b7, \x1b8  (cursor save/restore without bracket)
_ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]"  # Standard CSI sequences (colors, cursor, etc.)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (title, etc.) – BEL or ST terminator
    r"|\x1b\[\?[0-9;]*[a-zA-Z]"  # Private mode sequences (?2026h, ?25l, etc.)
    r"|\x1b\[>[0-9;]*[a-zA-Z]"  # Extended sequences (>1u, etc.)
    r"|\x1b\[<[a-zA-Z]"  # Pop sequences (<u)
    r"|\x1b[78]"  # Cursor save/restore (ESC 7, ESC 8)
    r"|\x07"  # Bell character
)

# Spinner characters used by Claude Code (dots, stars, etc.)
_SPINNER_CHARS = set("·✶✻✽✳✢*/-\\|●○◉◎◯◐◑◒◓⎿")


# ---------------------------------------------------------------------------
# Line-level cleaning
# ---------------------------------------------------------------------------


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences from *text*."""
    return _ANSI_ESCAPE_PATTERN.sub("", text)


def clean_terminal_line(line: str) -> str:
    """Clean a single terminal log line.

    Handles:
    - ANSI escape sequences (colors, cursor movement)
    - Carriage returns (spinner animations that overwrite lines)
    - Control characters
    """
    # Handle carriage returns: terminal overwrites from start of line.
    # Take only the content after the last carriage return.
    if "\r" in line:
        segments = line.split("\r")
        for segment in reversed(segments):
            stripped = strip_ansi_codes(segment).strip()
            if stripped:
                line = segment
                break
        else:
            line = segments[-1] if segments else ""

    line = strip_ansi_codes(line)

    # Remove control characters except tab and newline.
    line = "".join(c for c in line if c >= " " or c in "\t\n")
    return line


def _is_ui_noise(lower: str) -> bool:
    """Return True when *lower* (lowercased) is repetitive UI noise."""
    if "fiddle-faddling" in lower or "thinking" in lower or "running…" in lower:
        return True
    if lower.endswith("s)") and ("ought for" in lower or "hought for" in lower):
        return True
    if "bypass permissions" in lower or "shift+tab to cycle" in lower:
        return True
    return False


def _is_meaningful_short_line(stripped: str) -> bool:
    """Return True when a short line (<8 chars) is meaningful content."""
    if stripped.startswith(("─", "━")):
        return True
    if stripped in ("❯", ">", "$", "%"):
        return True
    if stripped.startswith(("⏺", "⎿")):
        return True
    if stripped.startswith(("✓", "✗", "•")) and len(stripped) > 4:
        return True
    return False


def is_spinner_fragment(line: str) -> bool:
    """Return True when *line* is a spinner animation fragment to filter."""
    stripped = line.strip()
    if not stripped:
        return True
    if all(c in _SPINNER_CHARS for c in stripped):
        return True
    if _is_ui_noise(stripped.lower()):
        return True
    if len(stripped) < 8:
        return not _is_meaningful_short_line(stripped)
    return False


def dedupe_consecutive_lines(lines: list[str]) -> list[str]:
    """Collapse consecutive duplicate or near-duplicate lines."""
    if not lines:
        return lines
    result = [lines[0]]
    for line in lines[1:]:
        prev = result[-1].strip()
        curr = line.strip()
        if curr == prev:
            continue
        if prev.startswith("─") and curr.startswith("─"):
            continue
        if prev in ("❯", ">") and curr in ("❯", ">"):
            continue
        result.append(line)
    return result


# ---------------------------------------------------------------------------
# Stream-JSON decoding (Claude --output-format stream-json)
# ---------------------------------------------------------------------------


def _parse_stream_json_record(raw: str) -> dict[str, Any] | None:
    candidate = raw.strip()
    if not candidate or not candidate.startswith("{"):
        return None
    try:
        record = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None
    return record


def _append_stream_event_text(record: dict[str, Any], text_parts: list[str]) -> bool:
    if record.get("type") != "stream_event":
        return False
    event = record.get("event")
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return True
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return True
    chunk = delta.get("text")
    if isinstance(chunk, str) and chunk:
        text_parts.append(chunk)
    return True


def _append_assistant_text(record: dict[str, Any], text_parts: list[str]) -> bool:
    if record.get("type") != "assistant":
        return False
    if text_parts:
        return True
    message = record.get("message")
    if not isinstance(message, dict):
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return True
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            text_parts.append(text)
    return True


def _append_result_text(record: dict[str, Any], text_parts: list[str]) -> bool:
    if record.get("type") != "result":
        return False
    if text_parts:
        return True
    result_text = record.get("result")
    if isinstance(result_text, str) and result_text:
        text_parts.append(result_text)
    return True


def extract_stream_json_text(lines: list[str]) -> list[str] | None:
    """Decode Claude stream-json log lines into plain transcript lines.

    Returns ``None`` when *lines* does not appear to be stream-json output.
    """
    saw_stream_json = False
    text_parts: list[str] = []

    for raw in lines:
        record = _parse_stream_json_record(raw)
        if record is None:
            continue
        if _append_stream_event_text(record, text_parts):
            saw_stream_json = True
            continue
        if _append_assistant_text(record, text_parts):
            saw_stream_json = True
            continue
        if _append_result_text(record, text_parts):
            saw_stream_json = True

    if not saw_stream_json:
        return None

    transcript = "".join(text_parts)
    if not transcript:
        return []
    return transcript.splitlines()


# ---------------------------------------------------------------------------
# CleaningLogWriter – pexpect logfile-compatible writer
# ---------------------------------------------------------------------------


def _is_consecutive_dup(prev_stripped: str, curr_stripped: str) -> bool:
    """Return True when two consecutive cleaned lines are duplicates."""
    if curr_stripped == prev_stripped:
        return True
    if prev_stripped.startswith("─") and curr_stripped.startswith("─"):
        return True
    if prev_stripped in ("❯", ">") and curr_stripped in ("❯", ">"):
        return True
    return False


class CleaningLogWriter:
    """File-like wrapper that cleans raw PTY bytes into readable text.

    Implements the pexpect ``logfile`` interface (``.write(bytes)``,
    ``.flush()``, ``.close()``).  Incoming bytes are buffered until a
    complete line (terminated by ``\\n``) is available, then the line is
    cleaned (ANSI stripped, carriage-return handling, spinner filtering,
    consecutive dedup) and written as UTF-8 text.
    """

    def __init__(self, path: Path) -> None:
        self._file = open(path, "w", encoding="utf-8")  # noqa: SIM115
        self._buffer = b""
        self._prev_stripped: str = ""
        self.name = str(path)

    # -- pexpect logfile interface ------------------------------------------

    def write(self, data: bytes) -> int:
        """Buffer *data* and write complete cleaned lines."""
        self._buffer += data
        while b"\n" in self._buffer:
            line_bytes, self._buffer = self._buffer.split(b"\n", 1)
            self._process_line(line_bytes)
        return len(data)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if self._buffer:
            self._process_line(self._buffer)
            self._buffer = b""
        self._file.close()

    # -- internal -----------------------------------------------------------

    def _process_line(self, line_bytes: bytes) -> None:
        line = line_bytes.decode("utf-8", errors="replace")
        cleaned = clean_terminal_line(line)
        if not cleaned.strip() or is_spinner_fragment(cleaned):
            return
        curr_stripped = cleaned.strip()
        if _is_consecutive_dup(self._prev_stripped, curr_stripped):
            return
        self._file.write(cleaned + "\n")
        self._prev_stripped = curr_stripped
