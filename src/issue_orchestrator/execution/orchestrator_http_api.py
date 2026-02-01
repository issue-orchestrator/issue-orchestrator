"""HTTP adapter for orchestrator control/query endpoints."""

from __future__ import annotations

from typing import Any, Callable
import threading

import httpx

from ..ports.orchestrator_api import OrchestratorApi


class OrchestratorHttpApi(OrchestratorApi):
    def __init__(
        self,
        base_url_provider: Callable[[], str],
        refresh_base_url: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url_provider = base_url_provider
        self._refresh_base_url = refresh_base_url
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._client_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url_provider()}{path}"
        try:
            with self._client_lock:
                response = self._client.request(method, url, json=json_body)
                response.raise_for_status()
                return response.json()
        except httpx.RequestError:
            if not self._refresh_base_url:
                raise
            refreshed_url = f"{self._refresh_base_url()}{path}"
            with self._client_lock:
                response = self._client.request(method, refreshed_url, json=json_body)
                response.raise_for_status()
                return response.json()

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


class OrchestratorAsyncHttpApi:
    def __init__(
        self,
        base_url_provider: Callable[[], str],
        refresh_base_url: Callable[[], str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url_provider = base_url_provider
        self._refresh_base_url = refresh_base_url
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url_provider()}{path}"
        try:
            response = await self._client.request(method, url, json=json_body)
            response.raise_for_status()
            return response.json()
        except httpx.RequestError:
            if not self._refresh_base_url:
                raise
            refreshed_url = f"{self._refresh_base_url()}{path}"
            response = await self._client.request(method, refreshed_url, json=json_body)
            response.raise_for_status()
            return response.json()

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/info")

    async def publish_jobs(self, issue_number: int | None = None) -> dict[str, Any]:
        path = "/api/publish-jobs"
        if issue_number is not None:
            path = f"{path}?issue_number={issue_number}"
        return await self._request("GET", path)

    async def excluded_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/excluded-issues")

    async def blocked_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/blocked-issues")

    async def stale_issues(self) -> dict[str, Any]:
        return await self._request("GET", "/api/stale-issues")

    async def dependency_problems(self) -> dict[str, Any]:
        return await self._request("GET", "/api/dependency-problems")

    async def history(self) -> dict[str, Any]:
        return await self._request("GET", "/api/history")

    async def pause(self) -> dict[str, Any]:
        return await self._request("POST", "/api/pause")

    async def resume(self) -> dict[str, Any]:
        return await self._request("POST", "/api/resume")

    async def refresh(self, inflight_stable_ids: list[str]) -> dict[str, Any]:
        return await self._request("POST", "/api/refresh", json_body={"inflight_stable_ids": inflight_stable_ids})

    async def shutdown(self, force: bool = False) -> dict[str, Any]:
        return await self._request("POST", f"/api/shutdown?force={str(force).lower()}")

    async def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/worktree/{issue_number}")

    async def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/manifest/{issue_number}")

    async def session_phases(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/phases/{issue_number}")

    async def session_claude_log(self, issue_number: int, limit: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/claude-log/{issue_number}?limit={limit}")

    async def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/session/orchestrator-log/{issue_number}")

    async def send(self, issue_number: int, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/send/{issue_number}", json_body={"text": text})

    async def kill(self, issue_number: int) -> dict[str, Any]:
        return await self._request("POST", f"/api/kill/{issue_number}")

    async def focus(self, issue_number: int) -> dict[str, Any]:
        return await self._request("POST", f"/api/focus/{issue_number}")

    async def doctor(self) -> dict[str, Any]:
        return await self._request("GET", "/api/doctor")
