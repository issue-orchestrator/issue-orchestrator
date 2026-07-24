"""Normalized open-issue facts used by the tech-lead dedup corpus.

GitHub owns whether an issue exists and is open.  These value objects are the
stable, infrastructure-free representation cached locally for deterministic
proposal deduplication.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Markdown/template scaffolding a tech-lead body almost always carries.  It is
# not topical evidence, so fingerprints and lexical comparisons both exclude it.
_BOILERPLATE_TOKENS = frozenset(
    {
        "problem",
        "summary",
        "context",
        "scope",
        "acceptance",
        "criteria",
        "related",
        "background",
        "proposed",
        "approach",
        "note",
        "notes",
        "issue",
        "pr",
        "tech",
        "lead",
    }
)

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "we",
        "our",
        "you",
        "your",
        "they",
        "their",
        "not",
        "no",
        "do",
        "does",
        "so",
        "from",
        "into",
        "when",
        "which",
        "should",
        "would",
        "could",
        "can",
        "will",
        "has",
        "have",
        "had",
        "than",
        "there",
        "here",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class OpenIssueRef:
    """One vetted open issue supplied to the proposal-dedup gate."""

    number: int
    title: str
    body: str = ""

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError("open issue number must be positive")


@dataclass(frozen=True)
class OpenIssueFingerprint:
    """Normalized cache row for one open issue."""

    issue: OpenIssueRef
    content_fingerprint: str

    def __post_init__(self) -> None:
        if not self.content_fingerprint:
            raise ValueError("content fingerprint must not be empty")


def substantive_tokens(text: str) -> tuple[str, ...]:
    """Return stable topical tokens with markdown punctuation and boilerplate removed."""

    return tuple(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS and token not in _BOILERPLATE_TOKENS
    )


def normalize_issue_text(text: str) -> str:
    """Canonical text stored in the rebuildable corpus cache."""

    return " ".join(substantive_tokens(text))


def build_open_issue_fingerprint(
    issue_number: int,
    title: str,
    body: str | None,
) -> OpenIssueFingerprint:
    """Normalize an open issue and derive its stable title/body fingerprint."""

    normalized_title = normalize_issue_text(title)
    normalized_body = normalize_issue_text(body or "")
    payload = f"{normalized_title}\0{normalized_body}".encode()
    return OpenIssueFingerprint(
        issue=OpenIssueRef(issue_number, normalized_title, normalized_body),
        content_fingerprint=hashlib.sha256(payload).hexdigest(),
    )
