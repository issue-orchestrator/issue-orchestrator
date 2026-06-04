"""Control API resume helper for agent completion commands."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from collections.abc import MutableMapping
from dataclasses import dataclass

from ...infra.env import get_env


@dataclass(frozen=True, slots=True)
class ApiHeader:
    """One HTTP header for an agent-scoped Control API callback."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ApiRequestHeaders:
    """Typed HTTP headers for an agent-scoped Control API callback."""

    headers: tuple[ApiHeader, ...]

    @classmethod
    def from_agent_environment(cls) -> "ApiRequestHeaders":
        values = [ApiHeader("Content-Type", "application/json")]
        token = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN")
        if token:
            values.append(ApiHeader("Authorization", f"Bearer {token}"))
        return cls(headers=tuple(values))

    def to_mutable_mapping(self) -> MutableMapping[str, str]:
        """Project to the mutable mapping required by ``urllib``."""
        return {header.name: header.value for header in self.headers}


def api_request_headers() -> ApiRequestHeaders:
    """Build Control API request headers for agent-scoped callbacks."""
    return ApiRequestHeaders.from_agent_environment()


def trigger_orchestrator_resume(verbose: bool = False) -> tuple[bool, str | None]:
    """Trigger the orchestrator to resume processing for this issue."""
    port = get_env("API_PORT") or os.environ.get("ORCHESTRATOR_API_PORT")
    issue_number = get_env("ISSUE_NUMBER") or os.environ.get("ORCHESTRATOR_ISSUE_NUMBER")

    if not port or not issue_number:
        missing: list[str] = []
        if not port:
            missing.append("ISSUE_ORCHESTRATOR_API_PORT")
        if not issue_number:
            missing.append("ISSUE_ORCHESTRATOR_ISSUE_NUMBER")
        return False, (
            f"Cannot resume: missing environment variables: {', '.join(missing)}. "
            "Completion record written. Resume processing from the web UI."
        )

    url = f"http://localhost:{port}/api/issues/{issue_number}/resume"

    if verbose:
        print(f"Triggering orchestrator resume for issue #{issue_number}...")

    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers=api_request_headers().to_mutable_mapping(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
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
