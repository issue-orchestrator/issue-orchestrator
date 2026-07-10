"""ADR-0031 owner boundary for triage session identity and completion effects.

Both triage variants (batch PR review and failure investigation) launch as
``issue-{N}`` sessions under the configured triage agent, so nothing about a
session's name distinguishes them. This module is the single owner for:

- **identity**: what makes a session a triage session (the config-declared
  triage agent), consolidating the checks previously duplicated in
  ``SessionLauncher`` and ``CompletionActionPlanner``;
- **flavor**: reading the launch-time :class:`TriageAssignment` that says
  which variant a session was given (manifest selection keys off it);
- **completion effects**: shaping the requested actions a triage completion
  may execute and classifying the benign "clean audit, nothing to publish"
  outcome so it is treated as success rather than a publish failure.
"""

import logging
from pathlib import Path

from ..domain.models import RequestedAction
from ..domain.triage_session import TRIAGE_ASSIGNMENT_FILENAME, TriageAssignment
from .completion_pr_collision import NoCommitsBetweenError

logger = logging.getLogger(__name__)


def is_triage_session(
    triage_review_agent: str | None, agent_type: str | None
) -> bool:
    """True when ``agent_type`` is the configured triage review agent."""
    return bool(triage_review_agent and agent_type == triage_review_agent)


def shape_requested_actions_for_triage(
    requested: tuple[RequestedAction, ...],
) -> tuple[RequestedAction, ...]:
    """Drop POST_COMMENT from a triage completion's requested actions.

    Triage prompts promise the orchestrator posts no comments; the generic
    "## Implementation" template would land on the tracking issue otherwise.
    PUSH_BRANCH/CREATE_PR stay: real prompt/doc improvements should publish.
    """
    return tuple(
        action for action in requested if action is not RequestedAction.POST_COMMENT
    )


def is_benign_triage_no_commits(
    action: RequestedAction, error: BaseException
) -> bool:
    """True when a triage CREATE_PR failed only because there is nothing to publish.

    A clean audit has nothing to publish; that is success, not publish-failure.
    """
    return action is RequestedAction.CREATE_PR and isinstance(
        error, NoCommitsBetweenError
    )


def read_triage_assignment(run_dir: Path) -> TriageAssignment | None:
    """Read the launch-time triage assignment from a session run directory.

    Returns None when the assignment file is absent (pre-upgrade sessions).
    Malformed content raises ValueError - callers decide the fail-safe.
    """
    path = run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
    if not path.exists():
        return None
    return TriageAssignment.read(path)
