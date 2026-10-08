"""Test doubles for a decision's steps beyond its item (#8691)."""

from __future__ import annotations

from typing import Any, NoReturn

from issue_orchestrator.control.tech_lead_decision_steps import DecisionStepsOwner


def _never(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("a decision without steps reaches no step owner")


def unused_decision_steps() -> DecisionStepsOwner:
    """The owner of a decision that carries no steps: any read or write fails the test."""
    return DecisionStepsOwner(
        read_issue=_never, read_pr=_never, list_milestones=_never, set_milestone=_never,
        write_body=_never, comment_marker_present=_never, apply_action=_never,
        require_authority=_never, rulings=_never, block=_never,  # type: ignore[arg-type]
        repo_slug="owner/repo",
    )
