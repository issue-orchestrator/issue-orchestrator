"""Classify push failures for completion publish retry policy."""

from __future__ import annotations


def is_non_fast_forward_push_failure(message: str) -> bool:
    lower = message.lower()
    return any(
        marker in lower
        for marker in (
            "non-fast-forward",
            "fetch first",
            "rejected",
            "stale info",
        )
    )
