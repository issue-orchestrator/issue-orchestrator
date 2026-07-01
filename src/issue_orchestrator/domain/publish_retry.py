"""Durable retry locators for a publish-failed issue.

When a completed coding session's publish (git push + PR creation) fails, the
heavy inputs a retry needs — the completion record and the session run
directory — already survive on disk in the worktree. Only the *pointers* to
them are ephemeral: they lived in the in-memory ``Session`` and were lost on
restart. :class:`PublishRetryLocators` captures exactly those pointers so a
publish-failed issue stays retryable across restarts. It is persisted per issue
when a publish fails, reconstructed on retry, and cleared once publish
succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .session_run import SessionRunAssets


@dataclass(frozen=True)
class PublishRetryLocators:
    """Pointers needed to re-run publish for a publish-failed issue.

    The republish itself re-reads the on-disk completion record (which carries
    the requested actions and outcome), so those are intentionally *not* stored
    here — only the locators required to find the work and drive
    ``CompletionProcessor.process``.
    """

    issue_number: int
    issue_title: str
    session_key: str
    worktree_path: str
    branch_name: str
    completion_path: str
    run_assets: SessionRunAssets
    agent_label: str | None = None
    pr_number: int | None = None
    # The original coding session's ``skip_review`` intent. Persisted because the
    # retry-publish reconciliation reuses the same review-routing policy as the
    # live completion path and cannot re-derive it from the worktree.
    skip_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "session_key": self.session_key,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "completion_path": self.completion_path,
            "run_assets": self.run_assets.to_dict(),
            "agent_label": self.agent_label,
            "pr_number": self.pr_number,
            "skip_review": self.skip_review,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishRetryLocators":
        # These fields are consumed as typed state later (label routing, republish
        # job inputs, review policy), not re-serialized as opaque JSON. A wrong
        # type here is store corruption, so validate rather than pass it through.
        return cls(
            issue_number=int(data["issue_number"]),
            issue_title=str(data["issue_title"]),
            session_key=str(data["session_key"]),
            worktree_path=str(data["worktree_path"]),
            branch_name=str(data["branch_name"]),
            completion_path=str(data["completion_path"]),
            run_assets=SessionRunAssets.from_dict(data["run_assets"]),
            agent_label=_optional_str(data.get("agent_label"), "agent_label"),
            pr_number=_optional_int(data.get("pr_number"), "pr_number"),
            skip_review=_require_bool(data.get("skip_review", False), "skip_review"),
        )


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(
            f"{field_name} must be a string or null, got {type(value).__name__}"
        )
    return value


def _optional_int(value: Any, field_name: str) -> int | None:
    # ``bool`` is an ``int`` subclass; reject it so a stray ``true`` is not
    # silently accepted as PR #1.
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(
            f"{field_name} must be an integer or null, got {type(value).__name__}"
        )
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(
            f"{field_name} must be a boolean, got {type(value).__name__}"
        )
    return value
