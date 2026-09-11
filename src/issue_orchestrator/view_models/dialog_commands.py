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

from typing import Annotated, Literal, Protocol, TypedDict

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


class SessionDiagnosticsActionContext(Protocol):
    """Fields required to derive dialog commands from one recorded session."""

    @property
    def issue_number(self) -> int: ...

    @property
    def run_dir(self) -> str: ...

    @property
    def session_settings_path(self) -> str: ...

    @property
    def claude_log_path(self) -> str: ...

    @property
    def claude_log_dir(self) -> str: ...

    @property
    def orchestrator_log(self) -> str: ...

    @property
    def diagnostic_path(self) -> str: ...

    @property
    def run_audit_path(self) -> str: ...

    @property
    def validation_path(self) -> str: ...

    @property
    def validation_output_path(self) -> str: ...

    @property
    def validation_stderr_path(self) -> str: ...


class DialogActionSectionPayload(TypedDict):
    """Presentation group containing serialized dialog actions."""

    title: str
    actions: list[object]


_SECTION_TITLES: tuple[tuple[SessionActionGroup, str], ...] = (
    ("validation_artifacts", "Validation Artifacts"),
    ("session_evidence", "Session Evidence"),
    ("diagnostics", "Diagnostics"),
)


def build_session_diagnostics_actions(
    ctx: SessionDiagnosticsActionContext,
) -> list[DialogAction]:
    """Derive typed commands for all available recorded-session artifacts."""
    actions: list[DialogAction] = []
    _append_open_path(actions, "Open Session Dir", ctx.run_dir, group="diagnostics")
    _append_open_path(
        actions,
        "Open Session Settings",
        ctx.session_settings_path,
        group="diagnostics",
    )
    _append_recording_actions(actions, ctx)
    _append_claude_log_actions(actions, ctx)
    _append_open_path(
        actions,
        "Open Claude Log Dir",
        ctx.claude_log_dir,
        group="session_evidence",
    )
    _append_orchestrator_log_action(actions, ctx)
    _append_open_path(
        actions,
        "Open Full Log",
        ctx.orchestrator_log,
        group="session_evidence",
    )
    _append_diagnostic_paths(actions, ctx)
    return actions


def append_session_diagnostics_action(
    actions: list[DialogAction],
    ctx: SessionDiagnosticsActionContext,
) -> None:
    """Append the command that opens full diagnostics when a run is available."""
    if ctx.run_dir:
        actions.append(
            DialogAction(
                command=OpenSessionDiagnosticsCommand(
                    issue_number=ctx.issue_number,
                    run_dir=ctx.run_dir,
                ),
                group="diagnostics",
            )
        )


def build_dialog_action_sections(
    actions: list[DialogAction],
) -> list[DialogActionSectionPayload]:
    """Bucket typed actions into their canonical display order."""
    grouped = {group: [] for group, _title in _SECTION_TITLES}
    for action in actions:
        grouped[action.group].append(action)

    return [
        {
            "title": title,
            "actions": [action.model_dump() for action in grouped[group]],
        }
        for group, title in _SECTION_TITLES
        if grouped[group]
    ]


def _append_recording_actions(
    actions: list[DialogAction],
    ctx: SessionDiagnosticsActionContext,
) -> None:
    if not ctx.run_dir:
        return
    actions.extend(
        (
            DialogAction(
                command=OpenSessionRecordingCommand(
                    label="View Session Recording",
                    issue_number=ctx.issue_number,
                    run_dir=ctx.run_dir,
                    error_surface="inline",
                ),
                group="session_evidence",
            ),
            DialogAction(
                command=CopySessionRecordingCommand(
                    issue_number=ctx.issue_number,
                    run_dir=ctx.run_dir,
                ),
                group="session_evidence",
            ),
        )
    )


def _append_claude_log_actions(
    actions: list[DialogAction],
    ctx: SessionDiagnosticsActionContext,
) -> None:
    if not ctx.claude_log_path:
        return
    if ctx.run_dir:
        actions.append(
            DialogAction(
                command=ViewClaudeLogCommand(
                    issue_number=ctx.issue_number,
                    run_dir=ctx.run_dir,
                ),
                group="session_evidence",
            )
        )
    _append_open_path(
        actions,
        "Open Claude Log File",
        ctx.claude_log_path,
        group="session_evidence",
    )


def _append_orchestrator_log_action(
    actions: list[DialogAction],
    ctx: SessionDiagnosticsActionContext,
) -> None:
    if ctx.run_dir:
        actions.append(
            DialogAction(
                command=OpenOrchestratorLogCommand(
                    issue_number=ctx.issue_number,
                    run_dir=ctx.run_dir,
                ),
                group="session_evidence",
            )
        )


def _append_diagnostic_paths(
    actions: list[DialogAction],
    ctx: SessionDiagnosticsActionContext,
) -> None:
    paths: tuple[tuple[str, str, SessionActionGroup], ...] = (
        ("Open Diagnostic", ctx.diagnostic_path, "diagnostics"),
        ("Open Run Audit", ctx.run_audit_path, "diagnostics"),
        ("Open Validation Record", ctx.validation_path, "validation_artifacts"),
        ("Open Validation Output", ctx.validation_output_path, "validation_artifacts"),
        ("Open Validation Stderr", ctx.validation_stderr_path, "validation_artifacts"),
    )
    for label, path, group in paths:
        _append_open_path(actions, label, path, group=group)


def _append_open_path(
    actions: list[DialogAction],
    label: str,
    path: str,
    *,
    group: SessionActionGroup,
) -> None:
    if path:
        actions.append(
            DialogAction(
                command=OpenPathCommand(label=label, path=path),
                group=group,
            )
        )


__all__ = [
    "CopySessionRecordingCommand",
    "DialogAction",
    "DialogActionCommand",
    "DialogActionSectionPayload",
    "ErrorSurface",
    "OpenOrchestratorLogCommand",
    "OpenPathCommand",
    "OpenSessionDiagnosticsCommand",
    "OpenSessionRecordingCommand",
    "SessionActionGroup",
    "SessionDiagnosticsActionContext",
    "ViewClaudeLogCommand",
    "append_session_diagnostics_action",
    "build_dialog_action_sections",
    "build_session_diagnostics_actions",
]
