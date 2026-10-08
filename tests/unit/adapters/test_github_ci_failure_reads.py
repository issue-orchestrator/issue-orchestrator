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


def test_a_long_log_is_asked_for_its_tail_by_byte_range(make_client) -> None:
    log = b"x" * 5000 + b"\nFAILED tests/test_end.py::test_last\n"
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://blob.example/log"})
        ranges.append(request.headers.get("Range"))
        if "Range" in request.headers:
            first = int(request.headers["Range"].removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=log[first:])
        return httpx.Response(200, content=log)

    tail = make_client(handler).get_actions_job_log_tail(77, max_bytes=1024)
    assert ranges == [None, f"bytes={len(log) - 1024}-"]
    assert tail.endswith("FAILED tests/test_end.py::test_last\n") and len(tail) == 1024


def test_a_server_ignoring_the_range_is_streamed_to_its_real_end(make_client) -> None:
    log = b"".join(f"line {n}\n".encode() for n in range(200000)) + b"the decisive line\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://blob.example/log"})
        return httpx.Response(200, content=log)  # Range ignored

    tail = make_client(handler).get_actions_job_log_tail(77, max_bytes=1024)
    assert tail.endswith("the decisive line\n") and len(tail) == 1024


def test_check_contexts_are_read_across_pages(make_client) -> None:
    pages: list[str | None] = []

    def node(n: int, conclusion: str) -> dict:
        return {"__typename": "CheckRun", "databaseId": n, "name": f"job {n}", "status": "COMPLETED",
                "conclusion": conclusion, "isRequired": True, "checkSuite": {"workflowRun": {"databaseId": 900}}}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        after = json.loads(request.content)["variables"]["after"]
        pages.append(after)
        nodes = [node(n, "SUCCESS") for n in range(100)] if after is None else [node(500, "FAILURE")]
        info = {"hasNextPage": after is None, "endCursor": "c1"}
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "headRefOid": "abc1234", "commits": {"nodes": [{"commit": {"oid": "abc1234",
                "statusCheckRollup": {"contexts": {"pageInfo": info, "nodes": nodes}}}}]}}}}})

    read = failed_checks_from_contexts(make_client(handler).get_failed_check_contexts(318))
    assert pages == [None, "c1"]
    assert read.checks == (FailedCheck("job 500", "FAILURE", True, 500, 900),)


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


def test_run_latest_attempt_lists_the_jobs_of_that_attempt(make_client) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/actions/runs/900"):
            return httpx.Response(200, json={"id": 900, "run_attempt": 2, "run_started_at": "2026-10-08T06:00:00Z"})
        return httpx.Response(200, json={"total_count": 2, "jobs": [{"id": 31}, {"id": 32}]})

    assert make_client(handler).get_actions_run_latest_attempt(900) == {
        "attempt": 2, "job_ids": [31, 32], "started_at": "2026-10-08T06:00:00Z",
    }
    assert paths == ["/repos/owner/repo/actions/runs/900", "/repos/owner/repo/actions/runs/900/attempts/2/jobs"]


def test_a_partial_attempt_job_listing_fails_loudly(make_client) -> None:
    from issue_orchestrator.adapters.github.errors import GitHubScanIncompleteError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/runs/900"):
            return httpx.Response(200, json={"id": 900, "run_attempt": 1, "run_started_at": "2026-10-08T06:00:00Z"})
        return httpx.Response(200, json={"total_count": 3, "jobs": [{"id": 31}]})

    with pytest.raises(GitHubScanIncompleteError, match="listed 1 of 3"):
        make_client(handler).get_actions_run_latest_attempt(900)



def test_checks_of_a_commit_that_is_not_the_head_are_refused(make_client) -> None:
    from issue_orchestrator.adapters.github.errors import GitHubScanIncompleteError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "headRefOid": "bbbbbbb", "commits": {"nodes": [{"commit": {"oid": "aaaaaaa",
                "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}]}}}}})

    with pytest.raises(GitHubScanIncompleteError, match="not its head"):
        make_client(handler).get_failed_check_contexts(318)
