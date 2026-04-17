"""Static prettifier for captured agent session output.

Session logs arrive as raw line-delimited JSON from whichever provider the
agent used — Claude's ``--output-format stream-json`` or Codex's ``exec --json``.
The web UI wants a single, clean transcript regardless of provider. This
module provides one pure entry point:

    prettify_session_log(raw_lines) -> list[str]

that dispatches to per-provider extractors (Claude stream-json, Codex JSON
event stream) and falls back to ANSI-stripped terminal output when the bytes
belong to neither. Per-provider extractors are pure functions so they are
trivial to test against real fixtures captured from production runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .terminal_cleaning import (
    clean_terminal_line,
    dedupe_consecutive_lines,
    extract_stream_json_text,
)

CodexItemRenderer = Callable[[dict[str, Any], str], "list[str] | None"]


def prettify_session_log(raw_lines: list[str]) -> list[str]:
    """Return a readable transcript for a captured session log.

    Tries provider-specific extractors in order. Each returns ``None`` if the
    input does not match its format — the dispatcher falls through cleanly.
    The last-ditch fallback is the existing terminal-cleaning pipeline.
    """
    claude = extract_claude_transcript(raw_lines)
    if claude is not None:
        return claude
    codex = extract_codex_transcript(raw_lines)
    if codex is not None:
        return codex
    return _cleaned_terminal_fallback(raw_lines)


def extract_claude_transcript(lines: list[str]) -> list[str] | None:
    """Decode Claude stream-json lines into a transcript.

    Thin wrapper preserving the pre-existing behaviour of
    :func:`extract_stream_json_text` while giving the dispatcher a consistent
    naming convention.
    """
    return extract_stream_json_text(lines)


def extract_codex_transcript(lines: list[str]) -> list[str] | None:
    """Decode Codex ``exec --json`` event stream into a transcript.

    Codex emits line-delimited JSON events with shapes like::

        {"type": "thread.started", ...}
        {"type": "turn.started"}
        {"type": "item.started",   "item": {"type": "command_execution", ...}}
        {"type": "item.completed", "item": {"type": "agent_message", "text": ...}}
        {"type": "item.completed", "item": {"type": "command_execution",
                                             "command": "...",
                                             "aggregated_output": "...",
                                             "exit_code": 0,
                                             "status": "completed"}}

    We keep only the terminal states for each item id (preferring
    ``item.completed`` over a previous ``item.started``) and render each item
    as human-facing transcript prose. Non-item meta events
    (``thread.started`` etc.) are silently skipped.

    Returns ``None`` when *lines* does not look like a Codex stream — so the
    dispatcher can try the next extractor. To avoid mis-classifying an
    arbitrary file that happens to contain a single ``thread.started`` line,
    we require at least one rendered item before committing to codex; a
    stream of pure meta events still commits (that's a legitimate empty
    codex session and shouldn't fall through to raw PTY decoding).
    """
    saw_codex_item = False
    saw_codex_meta = False
    items: dict[str, list[str]] = {}
    order: list[str] = []

    for raw in lines:
        record = _parse_json_line(raw)
        if record is None:
            continue
        event_type = record.get("type")
        if not isinstance(event_type, str):
            continue
        if event_type.startswith("item."):
            if _ingest_codex_event(record, items, order):
                saw_codex_item = True
        elif event_type in _CODEX_META_TYPES:
            saw_codex_meta = True

    if not saw_codex_item and not saw_codex_meta:
        return None
    transcript = _codex_transcript_lines(order, items)
    if not transcript and saw_codex_meta and not saw_codex_item:
        # Legitimate empty codex session (thread started but no items) —
        # emit a breadcrumb so the UI explains the blank instead of
        # silently showing nothing.
        return ["(codex session produced no items)"]
    return transcript


def _ingest_codex_event(
    record: dict[str, Any],
    items: dict[str, list[str]],
    order: list[str],
) -> bool:
    """Fold one item.* record into codex state; return True if an item rendered.

    Returning True from *item.** events that rendered content lets the outer
    dispatcher distinguish "committed codex" from "arbitrary line happened to
    contain thread.started" — meta events alone are insufficient to commit.
    """
    event_type = record["type"]
    item = record.get("item")
    if not isinstance(item, dict):
        return False
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return False
    rendered = _render_codex_item(item, event_type)
    if rendered is None:
        return False
    if item_id not in items:
        order.append(item_id)
    items[item_id] = rendered
    return True


def _codex_transcript_lines(
    order: list[str], items: dict[str, list[str]]
) -> list[str]:
    out: list[str] = []
    last_idx = len(order) - 1
    for idx, item_id in enumerate(order):
        rendered = items[item_id]
        if not rendered:
            continue
        out.extend(rendered)
        if idx != last_idx:
            out.append("")
    return out


def _parse_json_line(raw: str) -> dict[str, Any] | None:
    candidate = raw.strip()
    if not candidate or not candidate.startswith("{"):
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


_CODEX_META_TYPES = frozenset(
    {
        "thread.started",
        "thread.completed",
        "turn.started",
        "turn.completed",
        "turn.failed",
    }
)


def _render_codex_item(item: dict[str, Any], event_type: str) -> list[str] | None:
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return None
    renderer = _CODEX_ITEM_RENDERERS.get(item_type)
    if renderer is None:
        # Unknown item type — single-line breadcrumb so readers see *something*
        # rather than a silent gap. Same philosophy as the Claude path: never
        # drop content silently.
        return [f"(codex {item_type} event)"]
    return renderer(item, event_type)


def _render_codex_text_item(item: dict[str, Any], _event_type: str) -> list[str] | None:
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return None
    return text.splitlines() or [text]


def _render_codex_reasoning(item: dict[str, Any], _event_type: str) -> list[str] | None:
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return None
    return ["(reasoning)", *text.splitlines()]


def _render_codex_file_change(item: dict[str, Any], _event_type: str) -> list[str] | None:
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes:
        return None
    rendered = [f"(file change: {len(changes)} path(s))"]
    for entry in changes:
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str):
                rendered.append(f"  - {path}")
    return rendered


def _render_codex_command(
    item: dict[str, Any], event_type: str
) -> list[str] | None:
    command = item.get("command")
    if not isinstance(command, str) or not command:
        return None
    header = f"$ {command.strip()}"
    # For the started event we only know the command. Render it so in-flight
    # transcripts still show something; the later completed event overwrites.
    if event_type == "item.started":
        return [header]
    exit_code = item.get("exit_code")
    output = item.get("aggregated_output")
    lines = [header]
    if isinstance(output, str) and output.strip():
        lines.extend(output.rstrip().splitlines())
    if isinstance(exit_code, int) and exit_code != 0:
        lines.append(f"(exit code: {exit_code})")
    return lines


def _cleaned_terminal_fallback(raw_lines: list[str]) -> list[str]:
    """Strip terminal noise from raw bytes when no structured format matches."""
    cleaned = [clean_terminal_line(line) for line in raw_lines]
    cleaned = [line for line in cleaned if line.strip()]
    return dedupe_consecutive_lines(cleaned)


_CODEX_ITEM_RENDERERS: dict[str, CodexItemRenderer] = {
    "agent_message": _render_codex_text_item,
    "reasoning": _render_codex_reasoning,
    "command_execution": _render_codex_command,
    "file_change": _render_codex_file_change,
}
