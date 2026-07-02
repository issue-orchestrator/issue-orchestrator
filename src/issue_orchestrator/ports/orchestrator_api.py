"""Port for querying and controlling a running orchestrator instance."""

from __future__ import annotations

from typing import Any, Protocol


class OrchestratorApi(Protocol):
    """Abstracts orchestrator control/query surface for MCP and other clients."""

    def status(self) -> dict[str, Any]:
        ...

    def info(self) -> dict[str, Any]:
        ...

    def excluded_issues(self) -> dict[str, Any]:
        ...

    def blocked_issues(self) -> dict[str, Any]:
        ...

    def stale_issues(self) -> dict[str, Any]:
        ...

    def dependency_problems(self) -> dict[str, Any]:
        ...

    def history(self) -> dict[str, Any]:
        ...

    def pause(self) -> dict[str, Any]:
        ...

    def resume(self) -> dict[str, Any]:
        ...

    def refresh(self, inflight_stable_ids: list[str]) -> dict[str, Any]:
        ...

    def shutdown(
        self,
        *,
        reason: str,
        actor: str = "orchestrator_api.shutdown",
        force: bool = False,
    ) -> dict[str, Any]:
        ...

    def session_worktree(self, issue_number: int) -> dict[str, Any]:
        ...

    def session_manifest(self, issue_number: int) -> dict[str, Any]:
        ...

    def session_phases(self, issue_number: int) -> dict[str, Any]:
        ...

    def session_claude_log(self, issue_number: int, limit: int) -> dict[str, Any]:
        ...

    def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        ...

    def send(self, issue_number: int, text: str) -> dict[str, Any]:
        ...

    def kill(self, issue_number: int) -> dict[str, Any]:
        ...

    def focus(self, issue_number: int) -> dict[str, Any]:
        ...

    def doctor(self) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...
