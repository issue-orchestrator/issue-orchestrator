"""The empowered improver's toolbox only reads, and only what it was staged (#8001).

Escape vectors, each attempted against the real tool (real SQLite, real Git,
the real HTTP server and an MCP client): a write outside the run directory,
GitHub beyond the audited repository, files beyond the staged copies (the
operator's ``~/.claude``, the coordinator's directories), and a request
without the run's token.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from issue_orchestrator.contracts.improver_toolbox import TOOLBOX_DIRNAME
from issue_orchestrator.domain.improver_toolbox_policy import GitHubRead, ToolboxRefusal
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_toolbox import CALL_LOG, ImproverToolbox, serve_toolbox
from tests.unit.improver_toolbox_support import OUTSIDE_CANARY, build_toolbox_run

REPO = "porchpin/porchpin"


class FakeReads:
    def __init__(self) -> None:
        self.reads: list[GitHubRead] = []

    def get(self, read: GitHubRead) -> Any:
        self.reads.append(read)
        return {"number": 450, "title": "unschedulable", "milestone": None}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return build_toolbox_run(tmp_path)


def _toolbox(run_dir: Path, reads: FakeReads | None = None) -> ImproverToolbox:
    return ImproverToolbox(run_dir=run_dir, audited_repo=REPO, github=reads, runner=LocalCommandRunner())


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if ".git" not in p.parts}


def test_sql_reads_a_staged_store(run_dir: Path) -> None:
    answer = json.loads(_toolbox(run_dir).sql_query("timeline.sqlite", "SELECT event FROM timeline ORDER BY id"))

    assert answer == {"columns": ["event"], "rows": [["review.skipped"], ["rework.queued"]], "truncated": False}


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO timeline (event) VALUES ('x')",
        "DELETE FROM timeline",
        "CREATE TABLE x (a)",
        "DROP TABLE timeline",
        "PRAGMA journal_mode=WAL",
        "PRAGMA writable_schema=1",
        "VACUUM INTO '{outside}/vacuumed.sqlite'",
        "ATTACH DATABASE '{outside}/attached.sqlite' AS a",
        "ATTACH DATABASE '{outside}/outside.sqlite' AS a",
        "SELECT load_extension('/tmp/x')",
    ],
)
def test_sql_never_writes_nor_reaches_another_file(run_dir: Path, sql: str) -> None:
    outside = run_dir.parent
    before = _files(outside)

    with pytest.raises(ToolboxRefusal):
        _toolbox(run_dir).sql_query("timeline.sqlite", sql.format(outside=outside))

    assert _files(outside) == before
    with sqlite3.connect(run_dir / TOOLBOX_DIRNAME / "state" / "timeline.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM timeline").fetchone() == (2,)


@pytest.mark.parametrize(
    "database",
    ["../../outside.sqlite", str(Path("/") / "etc" / "x.sqlite"), "..\\outside.sqlite", ".hidden.sqlite",
     "missing.sqlite", "timeline.sqlite-wal", "settings.json"],
)
def test_sql_opens_only_a_staged_store(run_dir: Path, database: str) -> None:
    with pytest.raises(ToolboxRefusal):
        _toolbox(run_dir).sql_query(database, "SELECT 1")


def test_git_reads_the_clone_history(run_dir: Path) -> None:
    assert "first commit" in _toolbox(run_dir).git(["log", "--oneline"])


@pytest.mark.parametrize(
    "args",
    [
        ["log", "--output={outside}/written.txt"],
        ["log", "--outp={outside}/written.txt"],
        ["diff", "--no-index", "{outside}/secret.txt", "/dev/null"],
        ["blame", "--conte", "{outside}/secret.txt", "README.md"],
        ["grep", "-f", "{outside}/secret.txt"],
        ["grep", "-O", "x"],
        ["fetch", "origin"],
        ["config", "--global", "user.name", "x"],
    ],
)
def test_git_never_writes_outside_nor_reads_another_file(run_dir: Path, args: list[str]) -> None:
    outside = run_dir.parent
    before = _files(outside)

    with pytest.raises(ToolboxRefusal) as refused:
        _toolbox(run_dir).git([a.format(outside=outside) for a in args])

    assert OUTSIDE_CANARY not in str(refused.value)
    assert _files(outside) == before


def test_github_reads_only_the_audited_repository(run_dir: Path) -> None:
    reads = FakeReads()
    toolbox = _toolbox(run_dir, reads)

    answer = json.loads(toolbox.github_get("repos/porchpin/porchpin/issues/450"))
    for path in (
        "repos/issue-orchestrator/issue-orchestrator/issues/8001",
        "repos/porchpin/porchpin/../../issue-orchestrator/issue-orchestrator/issues/1",
        "graphql",
    ):
        with pytest.raises(ToolboxRefusal):
            toolbox.github_get(path)

    assert answer["number"] == 450
    assert [r.path for r in reads.reads] == ["/repos/porchpin/porchpin/issues/450"]


def test_with_github_off_nothing_is_read(run_dir: Path) -> None:
    with pytest.raises(ToolboxRefusal, match="off"):
        _toolbox(run_dir, None).github_get("repos/porchpin/porchpin/issues/1")


def _call(url: str, token: str | None, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go() -> tuple[bool, str]:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        async with (
            httpx.AsyncClient(headers=headers) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                text = "".join(getattr(c, "text", "") for c in result.content)
                return bool(result.isError), text

    return asyncio.run(go())


def test_the_server_serves_the_tools_to_the_token_holder_and_logs_each_call(run_dir: Path) -> None:
    reads = FakeReads()
    with serve_toolbox(_toolbox(run_dir, reads)) as endpoint:
        assert endpoint.url.startswith("http://127.0.0.1:")
        assert endpoint.token not in repr(endpoint)
        ok = _call(endpoint.url, endpoint.token, "github_get", {"path": "repos/porchpin/porchpin/issues/450"})
        refused = _call(
            endpoint.url, endpoint.token, "github_get", {"path": "repos/issue-orchestrator/issue-orchestrator/issues/1"}
        )
        sql = _call(endpoint.url, endpoint.token, "sql_query", {"database": "timeline.sqlite", "sql": "DELETE FROM timeline"})
        git = _call(endpoint.url, endpoint.token, "git", {"args": ["log", "--oneline"]})

    assert ok[0] is False and '"number": 450' in ok[1]
    assert refused[0] is True and "only porchpin/porchpin" in refused[1]
    assert sql[0] is True
    assert git[0] is False and "first commit" in git[1]
    calls = [json.loads(line) for line in (run_dir / CALL_LOG).read_text().splitlines()]
    assert [(c["tool"], c["outcome"].split(":")[0]) for c in calls] == [
        ("github_get", "ok"), ("github_get", "refused"), ("sql_query", "refused"), ("git", "ok"),
    ]
    assert len(reads.reads) == 1


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_the_server_refuses_a_request_without_the_runs_token(run_dir: Path, token: str | None) -> None:
    with serve_toolbox(_toolbox(run_dir, FakeReads())) as endpoint:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = httpx.post(
            endpoint.url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

    assert response.status_code == 401
    assert not (run_dir / CALL_LOG).exists()


def test_the_server_stops_with_the_run(run_dir: Path) -> None:
    with serve_toolbox(_toolbox(run_dir, FakeReads())) as endpoint:
        url = endpoint.url

    with pytest.raises(httpx.ConnectError):
        httpx.post(url, json={}, timeout=2)
