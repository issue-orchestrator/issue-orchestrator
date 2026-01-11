"""Local diagnostics helpers for issue-level failures."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiagnosticReference:
    """Reference to a local diagnostic file."""

    worktree_name: str
    relative_path: str


def write_issue_diagnostic(
    worktree: Path,
    issue_number: int,
    kind: str,
    summary: str,
    details: dict[str, Any],
) -> DiagnosticReference | None:
    """Write a diagnostic JSON file under the worktree.

    Returns a DiagnosticReference for use in user-facing comments,
    or None if the file could not be written.
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    diagnostics_dir = worktree / ".issue-orchestrator" / "diagnostics"
    filename = f"{timestamp}-{kind}-issue-{issue_number}.json"
    path = diagnostics_dir / filename

    payload = {
        "issue_number": issue_number,
        "kind": kind,
        "summary": summary,
        "timestamp": timestamp,
        "details": details,
    }

    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        relative_path = str(path.relative_to(worktree))
        return DiagnosticReference(worktree_name=worktree.name, relative_path=relative_path)
    except Exception as exc:
        logger.warning("Failed to write diagnostic file: %s", exc)
        return None
