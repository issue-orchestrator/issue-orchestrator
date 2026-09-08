"""Candidate writer shared by deterministic exchange subprocess fixtures."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.entrypoints.cli_tools.completion_submit import (
    submit_completion_file,
)


def write_and_submit_completion(path: Path, round_index: int) -> None:
    record = CompletionRecord(
        session_id="scripted-candidate",
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary=f"scripted coder round {round_index}",
        implementation=f"scripted coder round {round_index}",
        problems="None",
        requested_actions=[RequestedAction.PUSH_BRANCH],
    )
    path.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    # The standalone TUI keystroke test has no allocated run or intake task.
    # Every exchange integration test supplies this endpoint and must submit.
    port = os.environ.get("STUB_COMPLETION_API_PORT")
    if port and os.environ.get("ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY"):
        previous = os.environ.get("ISSUE_ORCHESTRATOR_API_PORT")
        os.environ["ISSUE_ORCHESTRATOR_API_PORT"] = port
        try:
            submit_completion_file(path)
        finally:
            if previous is None:
                del os.environ["ISSUE_ORCHESTRATOR_API_PORT"]
            else:
                os.environ["ISSUE_ORCHESTRATOR_API_PORT"] = previous
