"""Diagnostics for failed session worktree preparation."""

import json
import logging

from .worktree import WorktreePreparationError

logger = logging.getLogger(__name__)


def build_worktree_error_comment(error: WorktreePreparationError) -> str:
    """Build a comment explaining the worktree preparation failure."""
    safe_path = error.path.name
    return (
        f"## Worktree Preparation Failed\n\n"
        f"The orchestrator could not prepare the worktree for this issue.\n\n"
        f"**Error:** {error}\n\n"
        f"**Worktree path:** `{safe_path}`\n\n"
        f"**Details:** `.issue-orchestrator/diagnostics/worktree-prep.json` in that worktree; "
        f"look under your `worktree_base` (default: parent of the repo) for `{safe_path}`.\n\n"
        f"This usually means stale files from a previous session could not be deleted. "
        f"Please manually check and clean the worktree, then remove the `blocked-needs-human` label "
        f"to allow the orchestrator to retry."
    )


def write_worktree_diagnostic(error: WorktreePreparationError) -> None:
    """Write a local diagnostic file with full details."""
    diag_dir = error.path / ".issue-orchestrator" / "diagnostics"
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
        diag_path = diag_dir / "worktree-prep.json"
        diag_path.write_text(
            json.dumps(
                {
                    "issue_number": error.issue_number,
                    "worktree_path": str(error.path),
                    "error": str(error),
                },
                indent=2,
            )
            + "\n"
        )
    except Exception as exc:
        logger.warning("Failed to write worktree diagnostics: %s", exc)
