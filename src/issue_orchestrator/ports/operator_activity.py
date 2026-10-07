"""What people did by hand on the audited repository's GitHub (#8001).

The improver can find problems only the operator experiences (approval by
label removal, "before you approve" bookkeeping, hand merges) only if it sees
the operator's hand actions. The engine's own records hold few of them, so
:class:`OperatorActivitySource` reads them from GitHub, raw: every recent
issue event, comment and edit with its actor, for the domain policy
(:mod:`..domain.operator_interventions`) to tell people from automation.

A read is bounded (a few pages per source), so each source says whether it
reached back to the window's start (:class:`SourceRead`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Actor:
    """Who did it: a GitHub login and its account type (``User``, ``Bot``),
    and whether a GitHub App acted on its behalf."""

    login: str | None
    account_type: str | None
    via_app: bool = False


@dataclass(frozen=True)
class RepoEvent:
    """An issue or PR event (labeled, unlabeled, closed, reopened, renamed, ...)."""

    at: datetime
    event: str
    number: int
    actor: Actor
    #: The label of a labeled/unlabeled event.
    label: str | None
    #: The old and new title of a renamed event.
    rename: tuple[str, str] | None
    ref: str


@dataclass(frozen=True)
class RepoComment:
    """A comment as GitHub has it NOW: ``body`` is its current text, so a
    comment edited after ``updated_at`` would show text written later."""

    at: datetime
    updated_at: datetime
    number: int
    actor: Actor
    body: str
    ref: str


@dataclass(frozen=True)
class ContentEdit:
    at: datetime
    editor: Actor


@dataclass(frozen=True)
class Review:
    at: datetime
    state: str
    author: Actor


@dataclass(frozen=True)
class ItemActivity:
    """One issue or PR updated in the window: who opened it, edited its
    body, reviewed or merged it."""

    number: int
    is_pr: bool
    created_at: datetime
    author: Actor
    edits: tuple[ContentEdit, ...]
    reviews: tuple[Review, ...]
    merged_at: datetime | None
    merged_by: Actor | None
    ref: str


@dataclass(frozen=True)
class SourceRead:
    """One source's read: complete when it reached back to the window's
    start; otherwise why not (a page bound, a truncated nested list)."""

    name: str
    complete: bool
    detail: str


@dataclass(frozen=True)
class RepoActivityRead:
    events: tuple[RepoEvent, ...]
    comments: tuple[RepoComment, ...]
    items: tuple[ItemActivity, ...]
    sources: tuple[SourceRead, ...]


class MalformedActivity(ValueError):
    """GitHub answered a row without a field the read relies on: the read
    is refused whole, never staged with the row silently missing."""


class OperatorActivitySource(Protocol):
    def read(self, *, since: datetime, until: datetime) -> RepoActivityRead:
        """The repository's activity in ``[since, until]``; raises if it
        cannot be read, or if a row is malformed (:class:`MalformedActivity`)."""
        ...


__all__ = [
    "Actor",
    "ContentEdit",
    "ItemActivity",
    "MalformedActivity",
    "OperatorActivitySource",
    "RepoActivityRead",
    "RepoComment",
    "RepoEvent",
    "Review",
    "SourceRead",
]
