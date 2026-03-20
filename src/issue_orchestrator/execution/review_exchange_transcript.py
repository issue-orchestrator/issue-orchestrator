"""Helpers for parsing and filtering review-exchange transcripts."""

from __future__ import annotations

from dataclasses import dataclass
import re

_HEADER_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\] round=(?P<round_index>\d+) "
    r"role=(?P<role>[A-Za-z0-9_.-]+) section=(?P<section>[A-Za-z0-9_.-]+)\s*$"
)


@dataclass(frozen=True)
class ReviewExchangeTranscriptEntry:
    timestamp: str
    round_index: int
    role: str
    section: str
    content: str


def parse_review_exchange_transcript(text: str) -> list[ReviewExchangeTranscriptEntry]:
    """Parse transcript text into structured entries."""
    entries: list[ReviewExchangeTranscriptEntry] = []
    current_header: dict[str, str] | None = None
    current_content: list[str] = []

    def _flush() -> None:
        nonlocal current_header, current_content
        if current_header is None:
            return
        entries.append(
            ReviewExchangeTranscriptEntry(
                timestamp=current_header["timestamp"],
                round_index=int(current_header["round_index"]),
                role=current_header["role"],
                section=current_header["section"],
                content="\n".join(current_content).rstrip(),
            )
        )
        current_header = None
        current_content = []

    for raw_line in text.splitlines():
        match = _HEADER_RE.match(raw_line)
        if match:
            _flush()
            current_header = match.groupdict()
            continue
        if current_header is not None:
            current_content.append(raw_line)

    _flush()
    return entries


def filter_review_exchange_transcript(
    entries: list[ReviewExchangeTranscriptEntry],
    *,
    round_index: int | None = None,
    role: str | None = None,
) -> list[ReviewExchangeTranscriptEntry]:
    """Return entries matching the requested round/role slice."""
    filtered = entries
    if round_index is not None:
        filtered = [entry for entry in filtered if entry.round_index == round_index]
    if role:
        filtered = [entry for entry in filtered if entry.role == role]
    return filtered


def render_review_exchange_transcript(entries: list[ReviewExchangeTranscriptEntry]) -> str:
    """Render structured transcript entries back to display text."""
    blocks: list[str] = []
    for entry in entries:
        header = (
            f"[{entry.timestamp}] round={entry.round_index} "
            f"role={entry.role} section={entry.section}"
        )
        if entry.content:
            blocks.append(f"{header}\n{entry.content}".rstrip())
        else:
            blocks.append(header)
    if not blocks:
        return ""
    return "\n\n".join(blocks).rstrip() + "\n"
