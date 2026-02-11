"""Structured analysis of a completed session.

``SessionAnalysis`` is the output of the analyzer — a human-readable
diagnosis of what happened and what to do about it.  It's written to
``analysis.json`` alongside the manifest and used by the UI journey
timeline and post-mortem tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SessionAnalysis:
    """Structured diagnosis of a session outcome."""

    headline: str
    detail: str | None = None
    log_excerpt: str | None = None
    suggestions: list[str] = field(default_factory=list)
