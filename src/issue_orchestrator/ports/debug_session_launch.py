"""Port: an operator's debug session, as a command and one typed outcome (#9012).

The control API's debug button used to assemble its own launch - allocate the
run, persist the prompt, build the command, call the terminal - so it missed
what every other agent launch has: the provider readiness gate, the provider's
credentials and the terminal's exact preflight. The launch now belongs to the
launcher; the transport asks for it and maps the outcome. The outcome carries
facts, never a response body: how they are phrased is the transport's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .issue import Issue


class DebugSessionStatus(Enum):
    """How far a debug-session launch got. The transport maps only this."""

    LAUNCHED = "launched"
    #: The issue's worktree is gone; the issue needs to be re-run first.
    WORKTREE_MISSING = "worktree_missing"
    ISSUE_NOT_FOUND = "issue_not_found"
    NO_AGENT_LABEL = "no_agent_label"
    NO_AGENT_CONFIG = "no_agent_config"
    #: A debug session for the issue is already running.
    ALREADY_RUNNING = "already_running"
    #: Something the launch needs is not available now: the provider, the
    #: agent callback endpoint, the launch prompt's inputs, the run's ledger.
    UNAVAILABLE = "unavailable"
    #: The terminal could not exec the launch command; nothing was started.
    COMMAND_TOO_LONG = "command_too_long"
    #: The terminal never came up.
    SPAWN_FAILED = "spawn_failed"


@dataclass(frozen=True, slots=True)
class DebugSessionRequest:
    """One operator request for a debug session of an issue."""

    issue_number: int
    worktree_path: Path
    #: The issue as the facade's queue cache holds it; ``None`` on a cache
    #: miss, and the launch reads it from the repository host instead.
    cached_issue: "Issue | None"


@dataclass(frozen=True, slots=True)
class DebugSessionOutcome:
    """One settled debug-session launch, as facts rather than as a response."""

    status: DebugSessionStatus
    issue_number: int
    session_name: str
    worktree_path: Path
    agent_label: str | None = None
    #: Why the launch stopped, for the statuses that carry a cause.
    reason: str = ""

    @property
    def launched(self) -> bool:
        return self.status is DebugSessionStatus.LAUNCHED


__all__ = ["DebugSessionOutcome", "DebugSessionRequest", "DebugSessionStatus"]
