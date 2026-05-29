"""Review-exchange mode helpers shared by control-layer policy."""

from __future__ import annotations

FINAL_REVIEW_EXCHANGE_MODES = frozenset({"via-mcp", "via-local-loop"})


def is_final_review_exchange_mode(exchange_mode: str | None) -> bool:
    """Return whether the mode completes review before publish."""
    return exchange_mode in FINAL_REVIEW_EXCHANGE_MODES
