"""Blocked-issue and lifecycle-phase dialog view models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NotRequired, TypedDict


class BlockedIssuesDialogView(TypedDict):
    """Browser payload for the blocked-issues dialog."""

    title: str
    blocked_issues: object


class PhaseDialogView(TypedDict):
    """Browser payload for one selected lifecycle phase."""

    title: object
    issue_number: int
    phase: Mapping[str, object] | None
    phases: Sequence[Mapping[str, object]]


class PhaseEntry(TypedDict):
    """Fields used to select and title a lifecycle phase."""

    name: str
    display_name: NotRequired[str]


def build_blocked_issues_dialog(
    blocked_payload: Mapping[str, object],
) -> BlockedIssuesDialogView:
    """Project blocked issue data into its dialog payload."""
    return {
        "title": "Blocked Issues",
        "blocked_issues": blocked_payload.get("blocked_issues", []),
    }


def _find_last_phase_with_prefix(
    phases: Sequence[PhaseEntry],
    prefix: str,
) -> PhaseEntry | None:
    for phase in reversed(phases):
        if phase.get("name", "").startswith(prefix):
            return phase
    return None


def _select_phase(
    phases: Sequence[PhaseEntry],
    phase_key: str | None,
) -> PhaseEntry | None:
    if phase_key in ("in_progress", "rework"):
        return _find_last_phase_with_prefix(phases, "coding-")
    if phase_key in ("review", "tech_lead"):
        return _find_last_phase_with_prefix(phases, "review-")
    if phase_key:
        for phase in phases:
            if phase.get("name") == phase_key:
                return phase
    return None


def build_phase_dialog(
    phases_payload: Mapping[str, Sequence[PhaseEntry]],
    issue_number: int,
    phase_key: str | None,
) -> PhaseDialogView:
    """Select the requested lifecycle phase and project its dialog."""
    phases = phases_payload.get("phases", [])
    current = _select_phase(phases, phase_key)

    if current is None and phases:
        current = phases[-1]

    return {
        "title": current.get("display_name") if current else "Phase Details",
        "issue_number": issue_number,
        "phase": current,
        "phases": phases,
    }
