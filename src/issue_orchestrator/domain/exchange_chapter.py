"""Review-exchange chapter sidecar — boundary markers in a role's recording.

A chapter is "at this event-index in this role's terminal recording, this thing
started." Persistent-session review-exchanges write one chapter per cycle/role/
section so the session viewer can render a navigable outline ("Round 2 → Coder
Prompt", "Round 2 → Coder Feedback") and scrub the player straight to that
position in the recording.

Stored at: ``<run_dir>/<role>/chapters.json``

The file format is a single JSON object with a ``chapters`` array; the rest is
metadata so consumers can validate the file matches the recording they're
playing back. Append is implemented as read-modify-write under the adapter's
I/O lock — one persistent session writes its own chapters serially, so contention
is minimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHAPTER_SCHEMA_VERSION = 1

# Sections within a cycle: the orchestrator emits a chapter at each transition.
CHAPTER_SECTION_PROMPT = "prompt"
CHAPTER_SECTION_FEEDBACK = "feedback"
CHAPTER_SECTION_TIMEOUT = "timeout"


@dataclass(frozen=True)
class ExchangeChapter:
    """One boundary marker in a role's review-exchange recording."""

    cycle_index: int
    section: str
    recording_event_index: int
    recorded_at: str
    label: str


@dataclass(frozen=True)
class ExchangeChapterSidecar:
    """The on-disk shape of ``chapters.json``."""

    schema_version: int
    role: str
    exchange_run_id: str
    issue_number: int
    chapters: list[ExchangeChapter] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "exchange_run_id": self.exchange_run_id,
            "issue_number": self.issue_number,
            "chapters": [
                {
                    "cycle_index": ch.cycle_index,
                    "section": ch.section,
                    "recording_event_index": ch.recording_event_index,
                    "recorded_at": ch.recorded_at,
                    "label": ch.label,
                }
                for ch in self.chapters
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ExchangeChapterSidecar":
        chapters_raw = payload.get("chapters") or []
        if not isinstance(chapters_raw, list):
            raise ValueError(
                f"chapters.json: 'chapters' must be a list, got {type(chapters_raw).__name__}"
            )
        chapters = [
            ExchangeChapter(
                cycle_index=int(entry["cycle_index"]),
                section=str(entry["section"]),
                recording_event_index=int(entry["recording_event_index"]),
                recorded_at=str(entry["recorded_at"]),
                label=str(entry.get("label") or ""),
            )
            for entry in chapters_raw
        ]
        return cls(
            schema_version=int(payload.get("schema_version") or CHAPTER_SCHEMA_VERSION),
            role=str(payload["role"]),
            exchange_run_id=str(payload["exchange_run_id"]),
            issue_number=int(payload["issue_number"]),
            chapters=chapters,
        )
