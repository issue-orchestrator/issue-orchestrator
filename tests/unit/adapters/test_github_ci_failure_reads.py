"""The engine's GitHub reads and writes behind the CI-failure triage (#8692)."""

from __future__ import annotations

import httpx
import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.failed_checks import failed_checks_from_contexts
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.ports.pull_request_tracker import FailedCheck


@pytest.fixture
def make_client(monkeypatch):
    original = httpx.Client

    def make(handler) -> GitHubHttpClient:
        monkeypatch.setattr(
            httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
        )
        return GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="engine-token"))

    return make


def test_job_log_follows_the_redirect_without_the_credential_and_keeps_only_the_tail(make_client) -> None:
    seen: list[httpx.Request] = []
    log = b"".join(f"line {n}\n".encode() for n in range(20000))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            assert request.url.path == "/repos/owner/repo/actions/jobs/77/logs"
            return httpx.Response(302, headers={"Location": "https://blob.example/log?sig=x"})
        return httpx.Response(200, content=log)

    tail = make_client(handler).get_actions_job_log_tail(77, max_bytes=1024)

    assert tail.endswith("line 19999\n")
    assert len(tail.encode()) == 1024
    assert "Authorization" in seen[0].headers
    assert "Authorization" not in seen[1].headers  # the pre-signed URL never sees the token


def test_job_log_without_a_redirect_fails_loudly(make_client) -> None:
    client = make_client(lambda request: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubHttpError):
        client.get_actions_job_log_tail(77, max_bytes=1024)


def test_rerun_posts_to_the_rerun_failed_jobs_endpoint(make_client) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(201, content=b"")

    make_client(handler).rerun_failed_workflow_jobs(900)
    assert calls == [("POST", "/repos/owner/repo/actions/runs/900/rerun-failed-jobs")]


def test_failed_contexts_keep_only_completed_failures_with_their_actions_ids() -> None:
    read = failed_checks_from_contexts({"head_sha": "abc1234", "contexts": [
        {"__typename": "CheckRun", "databaseId": 11, "name": "tests", "status": "COMPLETED",
         "conclusion": "FAILURE", "isRequired": True, "checkSuite": {"workflowRun": {"databaseId": 900}}},
        {"__typename": "CheckRun", "databaseId": 12, "name": "lint", "status": "COMPLETED",
         "conclusion": "SUCCESS", "isRequired": True, "checkSuite": {"workflowRun": {"databaseId": 900}}},
        {"__typename": "CheckRun", "databaseId": 13, "name": "slow", "status": "IN_PROGRESS",
         "conclusion": None, "isRequired": True, "checkSuite": {"workflowRun": {"databaseId": 900}}},
        {"__typename": "CheckRun", "databaseId": 14, "name": "external", "status": "COMPLETED",
         "conclusion": "TIMED_OUT", "isRequired": False, "checkSuite": {"workflowRun": None}},
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR", "isRequired": True},
        {"__typename": "StatusContext", "context": "ci/ok", "state": "SUCCESS", "isRequired": True},
    ]})
    assert read.head_sha == "abc1234"
    assert read.checks == (
        FailedCheck("tests", "FAILURE", True, 11, 900),
        FailedCheck("external", "TIMED_OUT", False, None, None),
        FailedCheck("ci/legacy", "ERROR", True, None, None),
    )
