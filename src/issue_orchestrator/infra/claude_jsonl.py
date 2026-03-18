"""Shared Claude JSONL transcript parsing utilities."""

from __future__ import annotations

from typing import Any


def claude_jsonl_entry_preview_lines(entry: dict[str, Any]) -> list[str]:
    """Render concise preview lines from a Claude transcript entry."""
    entry_type = str(entry.get("type") or "")
    if entry_type == "stream_event":
        event = entry.get("event")
        if isinstance(event, dict):
            return _stream_event_preview_lines(event)
        return []
    if entry_type == "assistant":
        message = entry.get("message")
        if isinstance(message, dict):
            return _content_preview_lines(message.get("content"))
        return []
    if entry_type == "user":
        message = entry.get("message")
        if isinstance(message, dict):
            return _tool_result_preview_lines(message.get("content"))
    return []


def claude_jsonl_entry_replay_text(entry: dict[str, Any]) -> str:
    """Render terminal replay text from a Claude transcript entry."""
    entry_type = str(entry.get("type") or "")
    if entry_type == "stream_event":
        event = entry.get("event")
        if isinstance(event, dict):
            return _stream_event_replay_text(event)
        return ""
    if entry_type == "assistant":
        message = entry.get("message")
        if isinstance(message, dict):
            return _content_replay_text(message.get("content"))
        return ""
    if entry_type == "user":
        message = entry.get("message")
        if isinstance(message, dict):
            return _tool_result_replay_text(message.get("content"))
    return ""


def claude_tool_use_summary(tool_name: str, tool_input: Any) -> str:
    """Summarize a Claude tool-use entry for preview and replay surfaces."""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path"):
            summary = str(tool_input.get(key) or "").strip()
            if summary:
                return f"{tool_name}: {summary}"
    return tool_name


def _stream_event_preview_lines(event: dict[str, Any]) -> list[str]:
    text = _stream_event_text(event).strip()
    return [text] if text else []


def _stream_event_replay_text(event: dict[str, Any]) -> str:
    return _stream_event_text(event)


def _stream_event_text(event: dict[str, Any]) -> str:
    if str(event.get("type") or "") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return ""
    if str(delta.get("type") or "") != "text_delta":
        return ""
    return str(delta.get("text") or "")


def _content_preview_lines(content: Any) -> list[str]:
    preview_lines: list[str] = []
    for item in _iter_content_items(content):
        item_type = str(item.get("type") or "")
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                preview_lines.extend(line.strip() for line in text.splitlines() if line.strip())
        elif item_type == "tool_use":
            summary = claude_tool_use_summary(
                str(item.get("name") or "Tool").strip(),
                item.get("input"),
            )
            if summary:
                preview_lines.append(summary)
    return preview_lines


def _content_replay_text(content: Any) -> str:
    chunks: list[str] = []
    for item in _iter_content_items(content):
        item_type = str(item.get("type") or "")
        if item_type == "text":
            text = str(item.get("text") or "")
            if text:
                chunks.append(text if text.endswith("\n") else f"{text}\n")
        elif item_type == "tool_use":
            summary = claude_tool_use_summary(
                str(item.get("name") or "Tool").strip(),
                item.get("input"),
            )
            if summary:
                chunks.append(f"{summary}\n")
    return "".join(chunks)


def _tool_result_preview_lines(content: Any) -> list[str]:
    preview_lines: list[str] = []
    for item in _iter_content_items(content):
        if str(item.get("type") or "") != "tool_result":
            continue
        result_content = item.get("content")
        if isinstance(result_content, str):
            preview_lines.extend(_truncate_multiline_preview(result_content))
    return preview_lines


def _tool_result_replay_text(content: Any) -> str:
    chunks: list[str] = []
    for item in _iter_content_items(content):
        if str(item.get("type") or "") != "tool_result":
            continue
        result_content = item.get("content")
        if isinstance(result_content, str) and result_content.strip():
            chunks.append(result_content if result_content.endswith("\n") else f"{result_content}\n")
    return "".join(chunks)


def _iter_content_items(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _truncate_multiline_preview(text: str, *, max_lines: int = 8) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return lines
    return [*lines[:max_lines], "..."]
