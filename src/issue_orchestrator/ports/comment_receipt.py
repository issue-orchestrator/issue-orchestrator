"""Verified remote publication evidence, never inferred from a marker substring."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IssueCommentReceipt:
    """A fresh exact-body comment with provenance belonging to this credential."""

    comment_id: str
    url: str
    author_key: str
    body_sha256: str
