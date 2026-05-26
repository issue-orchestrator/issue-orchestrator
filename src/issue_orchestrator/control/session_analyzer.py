"""Stateless session analyzer — reads a RunManifest and produces a diagnosis.

Pure function: ``analyze(manifest) -> SessionAnalysis``.

Can be called inline during completion, on startup for crash recovery,
or via CLI for manual investigation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from issue_orchestrator.domain.artifact_contracts import ValidationFailed
from issue_orchestrator.domain.run_manifest import RunManifest
from issue_orchestrator.domain.session_analysis import SessionAnalysis

logger = logging.getLogger(__name__)

ANALYSIS_FILENAME = "analysis.json"

_MAX_HEADLINE = 120
_MAX_DETAIL = 300


def analyze(manifest: RunManifest) -> SessionAnalysis:
    """Produce a structured diagnosis from a run manifest."""
    outcome = manifest.outcome or ""

    if outcome == "blocked":
        return _analyze_blocked(manifest)
    if outcome in {"timeout", "timed_out"}:
        return _analyze_timeout(manifest)
    if outcome == "failed":
        return _analyze_failed(manifest)
    if outcome == "needs_human":
        return _analyze_needs_human(manifest)
    if outcome == "completed":
        return _analyze_completed(manifest)

    # Unknown or missing outcome
    return SessionAnalysis(
        headline=_trunc(f"Session ended with outcome: {outcome or 'unknown'}", _MAX_HEADLINE),
    )


def write_analysis(run_dir: Path, analysis: SessionAnalysis) -> None:
    """Write analysis.json to the run directory."""
    data: dict[str, str | list[str]] = {
        "headline": analysis.headline,
    }
    if analysis.detail is not None:
        data["detail"] = analysis.detail
    if analysis.log_excerpt is not None:
        data["log_excerpt"] = analysis.log_excerpt
    if analysis.suggestions:
        data["suggestions"] = list(analysis.suggestions)
    (run_dir / ANALYSIS_FILENAME).write_text(json.dumps(data, indent=2) + "\n")


def load_analysis(run_dir: Path) -> SessionAnalysis | None:
    """Load analysis.json from a run directory, or None if absent."""
    path = run_dir / ANALYSIS_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return SessionAnalysis(
            headline=data.get("headline", ""),
            detail=data.get("detail"),
            log_excerpt=data.get("log_excerpt"),
            suggestions=data.get("suggestions", []),
        )
    except Exception:
        logger.warning("[ANALYSIS] Failed to load %s", path, exc_info=True)
        return None


# ------------------------------------------------------------------
# Outcome-specific analyzers
# ------------------------------------------------------------------


def _analyze_blocked(m: RunManifest) -> SessionAnalysis:
    reason = m.blocked_reason or "no reason given"
    headline = f"Agent blocked: {reason}"

    parts: list[str] = []
    if m.attempted:
        parts.append(f"Tried: {m.attempted}")
    if m.blocked_by:
        issues = ", ".join(f"#{n}" for n in m.blocked_by)
        parts.append(f"Blocked by: {issues}")
    detail = ". ".join(parts) or None

    return SessionAnalysis(
        headline=_trunc(headline, _MAX_HEADLINE),
        detail=_trunc(detail, _MAX_DETAIL) if detail else None,
        log_excerpt=_tail(m.log_tail, 5),
        suggestions=["Remove blocked label to retry"],
    )


def _analyze_timeout(m: RunManifest) -> SessionAnalysis:
    runtime = m.runtime_minutes
    limit = m.timeout_minutes
    if runtime is not None and limit is not None:
        headline = f"Timed out after {runtime:.0f} min (limit: {limit} min)"
    elif runtime is not None:
        headline = f"Timed out after {runtime:.0f} min"
    else:
        headline = "Session timed out"

    return SessionAnalysis(
        headline=_trunc(headline, _MAX_HEADLINE),
        detail=_trunc("Agent did not invoke completion command before the timeout.", _MAX_DETAIL),
        log_excerpt=_tail(m.log_tail, 10),
        suggestions=[
            "Check transcript for what the agent was doing",
            "Consider increasing timeout if work was progressing",
        ],
    )


def _analyze_failed(m: RunManifest) -> SessionAnalysis:
    headline = "Session ended without completion command"

    parts: list[str] = []
    if m.problems:
        parts.append(m.problems)
    else:
        parts.append("Possible crash or interruption")
    detail = ". ".join(parts)

    return SessionAnalysis(
        headline=_trunc(headline, _MAX_HEADLINE),
        detail=_trunc(detail, _MAX_DETAIL),
        log_excerpt=_tail(m.log_tail, 10),
        suggestions=[
            "Check transcript for errors",
            "Remove needs-human label to retry",
        ],
    )


def _analyze_needs_human(m: RunManifest) -> SessionAnalysis:
    question = m.question or "No question provided"
    headline = f"Needs human input: {question}"

    return SessionAnalysis(
        headline=_trunc(headline, _MAX_HEADLINE),
        detail=_trunc(m.attempted, _MAX_DETAIL) if m.attempted else None,
        suggestions=["Answer on GitHub issue"],
    )


def _analyze_completed(m: RunManifest) -> SessionAnalysis:
    # Validation failure — branch off the typed outcome so the failure
    # reason cannot be a stale string left over from a previous attempt.
    outcome = m.validation_outcome
    if isinstance(outcome, ValidationFailed):
        return SessionAnalysis(
            headline=_trunc(
                f"Validation failed: {outcome.reason}", _MAX_HEADLINE
            ),
            detail=_trunc(m.problems, _MAX_DETAIL) if m.problems else None,
            log_excerpt=_tail(m.log_tail, 5),
            suggestions=["Check validation stderr"],
        )

    # Review with change requests
    if m.review_issues:
        headline = f"Reviewer requested changes: {m.review_issues}"
        detail = f"Risk: {m.risk_level}" if m.risk_level else None
        return SessionAnalysis(
            headline=_trunc(headline, _MAX_HEADLINE),
            detail=_trunc(detail, _MAX_DETAIL) if detail else None,
        )

    # Review approved
    if m.review_summary:
        headline = f"Reviewer approved: {m.review_summary}"
        return SessionAnalysis(
            headline=_trunc(headline, _MAX_HEADLINE),
        )

    # Normal completion
    impl = m.implementation or "No implementation details"
    headline = f"Completed: {impl}"
    detail = m.problems if m.problems else None

    return SessionAnalysis(
        headline=_trunc(headline, _MAX_HEADLINE),
        detail=_trunc(detail, _MAX_DETAIL) if detail else None,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _trunc(text: str | None, limit: int) -> str:
    """Truncate text to limit, adding ellipsis if needed."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _tail(log_tail: str | None, lines: int) -> str | None:
    """Extract the last N lines from a log tail string."""
    if not log_tail:
        return None
    all_lines = log_tail.strip().split("\n")
    return "\n".join(all_lines[-lines:])
