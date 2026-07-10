"""Typed command contract for dialog action buttons (issue #6327).

The validation / session-diagnostics dialogs render their action buttons as
typed ``data-lifecycle-command`` payloads that the shared frontend dispatcher
(``runLifecycleCommand``) consumes.  This module owns the producer→command
boundary: the Python dialog view models emit these typed commands directly, so
the JS renderer renders the provided command instead of reconstructing one from
a loose ``action.type`` dictionary.

Where the dialog semantics already match a canonical timeline command we reuse
that command — ``OpenSessionRecordingCommand`` (``open_session_recording``) —
rather than mint a parallel ``open_agent_log`` kind.  The remaining kinds are
dialog-only operations with no timeline counterpart and are modelled here as a
small discriminated union.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .lifecycle_semantics import OpenSessionRecordingCommand

# Where a dialog action's load failure should surface: a page-level toast, or
# inline inside the open dialog's message area.
ErrorSurface = Literal["toast", "inline"]

# Display section a dialog action button belongs to.
SessionActionGroup = Literal["validation_artifacts", "session_evidence", "diagnostics"]


class DialogCommandBase(BaseModel):
    """Base for the dialog-only typed commands (strict, frozen)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenPathCommand(DialogCommandBase):
    """Open a filesystem path via the OS host (external viewer)."""

    kind: Literal["open_path"] = "open_path"
    label: str
    path: str


class CopySessionRecordingCommand(DialogCommandBase):
    """Copy the session recording to the clipboard (local, no navigation)."""

    kind: Literal["copy_session_recording"] = "copy_session_recording"
    label: str = "Copy Session Recording"
    issue_number: int
    run_dir: str


class ViewClaudeLogCommand(DialogCommandBase):
    """Open the Claude log viewer modal for a run."""

    kind: Literal["view_claude_log"] = "view_claude_log"
    label: str = "View Claude Log"
    issue_number: int
    run_dir: str
    error_surface: ErrorSurface = "inline"


class OpenOrchestratorLogCommand(DialogCommandBase):
    """Fetch the issue-scoped orchestrator log and open it externally."""

    kind: Literal["open_orchestrator_log"] = "open_orchestrator_log"
    label: str = "Open Orchestrator Log"
    issue_number: int
    run_dir: str | None = None
    error_surface: ErrorSurface = "inline"


class OpenSessionDiagnosticsCommand(DialogCommandBase):
    """Open the full session diagnostics modal for an issue."""

    kind: Literal["open_session_diagnostics"] = "open_session_diagnostics"
    label: str = "Full Diagnostics"
    issue_number: int
    run_dir: str | None = None


DialogActionCommand = Annotated[
    OpenPathCommand
    | OpenSessionRecordingCommand
    | CopySessionRecordingCommand
    | ViewClaudeLogCommand
    | OpenOrchestratorLogCommand
    | OpenSessionDiagnosticsCommand,
    Field(discriminator="kind"),
]


class DialogAction(DialogCommandBase):
    """One dialog action button: a typed command plus its display section.

    The command carries everything the frontend dispatcher needs (kind, label,
    and any run context); ``group`` is a pure presentation concern used to bucket
    buttons into sections and is not part of the command semantics.
    """

    command: DialogActionCommand
    group: SessionActionGroup


__all__ = [
    "CopySessionRecordingCommand",
    "DialogAction",
    "DialogActionCommand",
    "ErrorSurface",
    "OpenOrchestratorLogCommand",
    "OpenPathCommand",
    "OpenSessionDiagnosticsCommand",
    "OpenSessionRecordingCommand",
    "SessionActionGroup",
    "ViewClaudeLogCommand",
]
