"""Control API resume helper for agent completion commands.

Generic callback plumbing (endpoint resolution, auth headers) lives in
:mod:`.agent_callback`; this module owns only the resume command.
"""

from __future__ import annotations

from ...domain.completion_intake import CompletionIntakeReceipt

import json
import os
import urllib.error

from dataclasses import dataclass

from ...infra.env import get_env
from .agent_callback import resolve_control_api_port


@dataclass(frozen=True, slots=True)
class ResumeTarget:
    """Typed Control API endpoint identity for an agent resume callback."""

    port: str
    issue_number: str

    @classmethod
    def from_agent_environment(cls) -> "ResumeTarget":
        raw_issue_number = get_env("ISSUE_NUMBER") or os.environ.get(
            "ORCHESTRATOR_ISSUE_NUMBER"
        )
        port = resolve_control_api_port() or ""
        issue_number = raw_issue_number.strip() if raw_issue_number else ""
        missing: list[str] = []
        if port == "":
            missing.append("ISSUE_ORCHESTRATOR_API_PORT")
        if issue_number == "":
            missing.append("ISSUE_ORCHESTRATOR_ISSUE_NUMBER")
        if missing:
            raise ValueError(
                f"missing environment variables: {', '.join(missing)}"
            )
        return cls(port=port, issue_number=issue_number)

    def url(self) -> str:
        return f"http://localhost:{self.port}/api/issues/{self.issue_number}/resume"


def trigger_orchestrator_resume(
    verbose: bool = False, *, receipt: "CompletionIntakeReceipt"
) -> tuple[bool, str | None]:
    """Trigger the orchestrator to resume processing for this issue."""
    try:
        target = ResumeTarget.from_agent_environment()
        from ...contracts.ui_openapi_models import CompletionIntakeReceiptPayload
        from .completion_submit import CAPABILITY_ENV

        capability = os.environ.get(CAPABILITY_ENV)
        if not capability:
            raise ValueError("completion intake capability required")
        request_body = CompletionIntakeReceiptPayload(
            entry_id=receipt.entry_id, content_sha256=receipt.content_sha256
        )
    except ValueError as exc:
        return False, (
            f"Cannot resume: {exc}. Completion record written. "
            "Resume processing from the web UI."
        )

    if verbose:
        print(f"Triggering orchestrator resume for issue #{target.issue_number}...")

    try:
        from .completion_submit import post_completion_command

        result = json.loads(
            post_completion_command(
                f"/api/issues/{int(target.issue_number)}/resume",
                request_body.model_dump_json().encode(),
                timeout=120,
            )
        )
        if result.get("success"):
            return True, None
        return False, result.get("error", "Unknown error from orchestrator")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            error_data = json.loads(body)
            return False, error_data.get("error", f"HTTP {exc.code}: {body}")
        except Exception:
            return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"Could not reach orchestrator API: {exc}"
    except Exception as exc:
        return False, f"Resume request failed: {exc}"
