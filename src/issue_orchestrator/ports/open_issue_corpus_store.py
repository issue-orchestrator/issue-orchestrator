"""Port for the rebuildable tech-lead open-issue fingerprint cache."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..domain.open_issue_corpus import OpenIssueFingerprint, OpenIssueRef


@dataclass(frozen=True)
class OpenIssueCorpusSnapshot:
    """One atomically loaded cache generation and its GitHub delta cursor."""

    entries: tuple[OpenIssueFingerprint, ...]
    watermark: str

    def __post_init__(self) -> None:
        if not self.watermark:
            raise ValueError("open-issue corpus watermark must not be empty")

    @property
    def issues(self) -> tuple[OpenIssueRef, ...]:
        return tuple(entry.issue for entry in self.entries)


class OpenIssueCorpusStore(Protocol):
    """Stores normalized open issues; GitHub remains the source of truth."""

    def load(self) -> OpenIssueCorpusSnapshot | None:
        """Load the last successful cache generation, or ``None`` before first sync."""
        ...

    def replace_all(
        self,
        entries: Sequence[OpenIssueFingerprint],
        *,
        watermark: str,
    ) -> None:
        """Atomically replace the corpus after a complete GitHub rebuild."""
        ...

    def apply_delta(
        self,
        upserts: Sequence[OpenIssueFingerprint],
        *,
        evict_issue_numbers: Sequence[int],
        watermark: str,
    ) -> None:
        """Atomically upsert changed open issues, evict closed issues, and advance."""
        ...


class InMemoryOpenIssueCorpusStore:
    """Deterministic in-memory implementation for control-layer tests."""

    def __init__(self) -> None:
        self._entries: dict[int, OpenIssueFingerprint] = {}
        self._watermark: str | None = None

    def load(self) -> OpenIssueCorpusSnapshot | None:
        if self._watermark is None:
            return None
        return OpenIssueCorpusSnapshot(
            tuple(self._entries[number] for number in sorted(self._entries)),
            self._watermark,
        )

    def replace_all(
        self,
        entries: Sequence[OpenIssueFingerprint],
        *,
        watermark: str,
    ) -> None:
        entry_map = index_open_issue_entries(entries)
        cursor = validate_corpus_watermark(watermark)
        self._entries = entry_map
        self._watermark = cursor

    def apply_delta(
        self,
        upserts: Sequence[OpenIssueFingerprint],
        *,
        evict_issue_numbers: Sequence[int],
        watermark: str,
    ) -> None:
        upsert_map = index_open_issue_entries(upserts)
        evictions = validate_open_issue_evictions(evict_issue_numbers)
        overlap = set(upsert_map).intersection(evictions)
        if overlap:
            raise ValueError(
                f"open-issue delta both upserts and evicts issue(s): {sorted(overlap)}"
            )
        cursor = validate_corpus_watermark(watermark)
        self._entries.update(upsert_map)
        for issue_number in evictions:
            self._entries.pop(issue_number, None)
        self._watermark = cursor


def index_open_issue_entries(
    entries: Sequence[OpenIssueFingerprint],
) -> dict[int, OpenIssueFingerprint]:
    by_number: dict[int, OpenIssueFingerprint] = {}
    for entry in entries:
        existing = by_number.get(entry.issue.number)
        if existing is not None and existing != entry:
            raise ValueError(
                f"conflicting open-issue cache rows for issue #{entry.issue.number}"
            )
        by_number[entry.issue.number] = entry
    return by_number


def validate_open_issue_evictions(issue_numbers: Sequence[int]) -> set[int]:
    evictions = set(issue_numbers)
    if any(issue_number <= 0 for issue_number in evictions):
        raise ValueError("open-issue eviction numbers must be positive")
    return evictions


def validate_corpus_watermark(watermark: str) -> str:
    if not watermark:
        raise ValueError("open-issue corpus watermark must not be empty")
    return watermark
