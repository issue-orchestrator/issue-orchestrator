"""Shared timeline trace flag helpers."""

from __future__ import annotations

import os


def is_timeline_trace_enabled() -> bool:
    """Return whether timeline trace logging is enabled."""
    value = os.environ.get("ISSUE_ORCHESTRATOR_TIMELINE_TRACE", "")
    return value.lower() in {"1", "true", "yes", "on"}
