"""Typed payload fragments shared by the public MCP tool boundary."""

from collections.abc import Mapping
from typing import Literal, NotRequired, TypeAlias, TypedDict


class McpErrorPayload(TypedDict):
    """Structured error returned by MCP tools."""

    message: str
    type: str


class McpUiHintPayload(TypedDict):
    """Optional client navigation hint attached to a failed MCP start."""

    kind: Literal["doctor"]
    url: NotRequired[str]


class McpIssueRetryCommitted(TypedDict):
    """An issue-scoped operator retry that committed its label transition."""

    success: Literal[True]
    message: str
    removed_labels: list[str]


class McpIssueRetryHeld(TypedDict):
    """An operator retry that left the issue blocked."""

    success: Literal[False]
    error: str
    removed_labels: list[str]
    failed_labels: list[str]
    held_by: NotRequired[list[str]]


McpIssueRetryOutcome: TypeAlias = McpIssueRetryCommitted | McpIssueRetryHeld


def parse_issue_retry_outcome(payload: Mapping[str, object]) -> McpIssueRetryOutcome:
    """Validate the operator command's committed or refused JSON payload."""
    removed = payload.get("removed_labels")
    if not isinstance(removed, list) or any(not isinstance(label, str) for label in removed):
        raise ValueError("issue retry response missing removed_labels")
    if payload.get("success") is True:
        message = payload.get("message")
        if not isinstance(message, str):
            raise ValueError("issue retry response missing message")
        return McpIssueRetryCommitted(success=True, message=message, removed_labels=removed)
    if payload.get("success") is False:
        error = payload.get("error")
        failed = payload.get("failed_labels")
        held_by = payload.get("held_by")
        if (
            not isinstance(error, str)
            or not isinstance(failed, list)
            or any(not isinstance(label, str) for label in failed)
            or (
                held_by is not None
                and (
                    not isinstance(held_by, list)
                    or any(not isinstance(label, str) for label in held_by)
                )
            )
        ):
            raise ValueError("issue retry refusal response drifted")
        result = McpIssueRetryHeld(
            success=False, error=error, removed_labels=removed, failed_labels=failed,
        )
        if held_by is not None:
            result["held_by"] = held_by
        return result
    raise ValueError("issue retry response missing success")
