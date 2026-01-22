"""HTTP adapter for orchestrator control/query endpoints."""

from __future__ import annotations

from typing import Any, Callable
import json
import urllib.error
import urllib.request

from ..ports.orchestrator_api import OrchestratorApi


class OrchestratorHttpApi(OrchestratorApi):
    def __init__(self, base_url_provider: Callable[[], str], timeout_seconds: float = 10.0) -> None:
        self._base_url_provider = base_url_provider
        self._timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url_provider()}{path}"
        data = None
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8").strip()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/status")

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/api/info")

    def publish_jobs(self, issue_number: int | None = None) -> dict[str, Any]:
        path = "/api/publish-jobs"
        if issue_number is not None:
            path = f"{path}?issue_number={issue_number}"
        return self._request("GET", path)

    def excluded_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/excluded-issues")

    def blocked_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/blocked-issues")

    def stale_issues(self) -> dict[str, Any]:
        return self._request("GET", "/api/stale-issues")

    def dependency_problems(self) -> dict[str, Any]:
        return self._request("GET", "/api/dependency-problems")

    def history(self) -> dict[str, Any]:
        return self._request("GET", "/api/history")

    def pause(self) -> dict[str, Any]:
        return self._request("POST", "/api/pause")

    def resume(self) -> dict[str, Any]:
        return self._request("POST", "/api/resume")

    def refresh(self, inflight_stable_ids: list[str]) -> dict[str, Any]:
        return self._request("POST", "/api/refresh", json_body={"inflight_stable_ids": inflight_stable_ids})

    def shutdown(self, force: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/api/shutdown?force={str(force).lower()}")

    def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/worktree/{issue_number}")

    def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/manifest/{issue_number}")

    def session_phases(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/phases/{issue_number}")

    def session_claude_log(self, issue_number: int, limit: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/claude-log/{issue_number}?limit={limit}")

    def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/api/session/orchestrator-log/{issue_number}")

    def send(self, issue_number: int, text: str) -> dict[str, Any]:
        return self._request("POST", f"/api/send/{issue_number}", json_body={"text": text})

    def kill(self, issue_number: int) -> dict[str, Any]:
        return self._request("POST", f"/api/kill/{issue_number}")

    def focus(self, issue_number: int) -> dict[str, Any]:
        return self._request("POST", f"/api/focus/{issue_number}")

    def doctor(self) -> dict[str, Any]:
        return self._request("GET", "/api/doctor")
