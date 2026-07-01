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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishRetryLocators":
        return cls(
            issue_number=int(data["issue_number"]),
            issue_title=str(data["issue_title"]),
            session_key=str(data["session_key"]),
            worktree_path=str(data["worktree_path"]),
            branch_name=str(data["branch_name"]),
            completion_path=str(data["completion_path"]),
            run_assets=SessionRunAssets.from_dict(data["run_assets"]),
            agent_label=data.get("agent_label"),
            pr_number=data.get("pr_number"),
        )
