"""Typed payload fragments shared by the public MCP tool boundary."""

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


class McpToolErrorResult(TypedDict):
    """Transport or unexpected error converted by the MCP error boundary."""

    error: McpErrorPayload


McpIssueRetryOutcome: TypeAlias = McpIssueRetryCommitted | McpIssueRetryHeld
McpIssueRetryResult: TypeAlias = McpIssueRetryOutcome | McpToolErrorResult
