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
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from issue_orchestrator.contracts.improver_toolbox import TOOLBOX_DIRNAME
from issue_orchestrator.domain.improver_toolbox_policy import GitHubRead, ToolboxRefusal
from issue_orchestrator.execution.improver_toolbox import CALL_LOG, ImproverToolbox, serve_toolbox
from tests.unit.improver_toolbox_support import OUTSIDE_CANARY, build_toolbox_run

REPO = "porchpin/porchpin"


class FakeReads:
    def __init__(self) -> None:
        self.reads: list[GitHubRead] = []

    def get(self, read: GitHubRead, *, max_bytes: int) -> Any:
        self.reads.append(read)
        return {"number": 450, "title": "unschedulable", "milestone": None}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return build_toolbox_run(tmp_path)


def _toolbox(run_dir: Path, reads: FakeReads | None = None, hidden: frozenset[int] = frozenset()) -> ImproverToolbox:
    return ImproverToolbox(run_dir=run_dir, audited_repo=REPO, github=reads, hidden_issues=hidden)


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


class SearchReads:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def get(self, read: GitHubRead, *, max_bytes: int) -> Any:
        return {"total_count": len(self.items), "items": self.items}


@pytest.mark.parametrize(
    ("items", "allowed"),
    [
        ([{"number": 1, "repository_url": "https://api.github.com/repos/porchpin/porchpin"}], True),
        ([{"sha": "a", "repository": {"url": "https://api.github.com/repos/Porchpin/Porchpin"}}], True),
        ([], True),
        # r1 F1, the second wall: a search answer naming another repository.
        ([{"number": 1, "repository_url": "https://api.github.com/repos/porchpin/porchpin"},
          {"number": 8001, "repository_url": "https://api.github.com/repos/issue-orchestrator/issue-orchestrator"}], False),
        ([{"number": 2}], False),
        ([{"sha": "b", "repository": {"url": "https://api.github.com/repos/x/porchpin/porchpin2"}}], False),
    ],
)
def test_a_search_answer_outside_the_audited_repository_is_refused_whole(
    run_dir: Path, items: list[dict[str, Any]], allowed: bool
) -> None:
    toolbox = _toolbox(run_dir, SearchReads(items))  # type: ignore[arg-type]

    def read() -> str:
        return toolbox.github_get("search/issues", {"q": "repo:porchpin/porchpin label:x"})

    if allowed:
        assert json.loads(read())["total_count"] == len(items)
    else:
        with pytest.raises(ToolboxRefusal, match="outside porchpin/porchpin"):
            read()


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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT randomblob(16000000)",
        "SELECT zeroblob(2000000000)",
        "SELECT printf('%.*c', 50000000, 'x')",
        "WITH RECURSIVE r(s) AS (SELECT 'x' UNION ALL SELECT s || s FROM r) SELECT s FROM r",
    ],
)
def test_a_query_producing_an_oversized_value_is_refused_before_it_is_built(run_dir: Path, sql: str) -> None:
    """r3 F2: SQLite's length limit stops the value; the orchestrator never
    holds it."""
    with pytest.raises(ToolboxRefusal, match="too big|failed"):
        _toolbox(run_dir).sql_query("timeline.sqlite", sql)


def test_an_answer_stops_at_its_byte_budget(run_dir: Path) -> None:
    import issue_orchestrator.execution.improver_toolbox as toolbox_module

    sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 900) SELECT randomblob(60000) FROM n"

    answer = json.loads(_toolbox(run_dir).sql_query("timeline.sqlite", sql))

    assert answer["truncated"] is True
    assert len(answer["rows"]) * 60_000 <= toolbox_module.MAX_SQL_FETCH_BYTES


class AnswerReads:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def get(self, read: GitHubRead, *, max_bytes: int) -> Any:
        return self.answer


@pytest.mark.parametrize(
    ("path", "params", "answer"),
    [
        ("repos/porchpin/porchpin/issues/7592", None, {}),
        ("repos/porchpin/porchpin/issues/7592/timeline", None, []),
        ("repos/porchpin/porchpin/pulls/7592/reviews", None, []),
        # Listed, searched or referenced: refused all the same.
        ("repos/porchpin/porchpin/issues", {"state": "open"}, [{"number": 1, "title": "a"}, {"number": 7592, "title": "hidden"}]),
        ("search/issues", {"q": "repo:porchpin/porchpin x"},
         {"items": [{"number": 7592, "title": "hidden", "repository_url": "https://api.github.com/repos/porchpin/porchpin"}]}),
        ("repos/porchpin/porchpin/issues/1/timeline", None,
         [{"event": "cross-referenced", "source": {"issue": {"number": 7592, "title": "hidden"}}}]),
        # r6 F1: a repository-wide comment names its issue only by URL.
        ("repos/porchpin/porchpin/issues/comments", None,
         [{"id": 1, "issue_url": "https://api.github.com/repos/porchpin/porchpin/issues/7592", "body": "canary"}]),
        ("repos/porchpin/porchpin/pulls/comments", None,
         [{"id": 2, "html_url": "https://github.com/porchpin/porchpin/pull/7592#discussion_r9", "body": "canary"}]),
    ],
)
def test_a_blind_runs_hidden_issue_is_never_shown(run_dir: Path, path: str, params: dict | None, answer: Any) -> None:
    """r5 F1: direct reads, lists, searches and references of a hidden issue."""
    toolbox = _toolbox(run_dir, AnswerReads(answer), hidden=frozenset({7592}))  # type: ignore[arg-type]

    with pytest.raises(ToolboxRefusal, match="blind run"):
        toolbox.github_get(path, params)


def test_an_unhidden_issue_with_the_same_shape_is_shown(run_dir: Path) -> None:
    toolbox = _toolbox(run_dir, AnswerReads([{"number": 1, "title": "a"}]), hidden=frozenset({7592}))  # type: ignore[arg-type]

    assert json.loads(toolbox.github_get("repos/porchpin/porchpin/issues"))[0]["number"] == 1


def test_git_output_is_bounded_while_it_streams(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """r5 F2: ``git show HEAD:big.bin`` must never be held whole."""
    import issue_orchestrator.execution.improver_toolbox as toolbox_module

    repo = run_dir / "toolbox" / "repo"
    (repo / "big.bin").write_bytes(b"x" * 3_000_000)
    subprocess.run(["git", "add", "big.bin"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "big"], cwd=repo, check=True)
    monkeypatch.setattr(toolbox_module, "MAX_ANSWER_CHARS", 10_000)
    captured: list[int] = []
    real = toolbox_module._bounded_run

    def spy(*args: Any, **kwargs: Any) -> Any:
        done = real(*args, **kwargs)
        captured.append(len(done.stdout))
        return done

    monkeypatch.setattr(toolbox_module, "_bounded_run", spy)

    answer = _toolbox(run_dir).git(["show", "HEAD:big.bin"])

    assert captured == [10_000]
    assert answer.endswith("narrow the request>")


def test_no_single_row_can_exceed_the_fetch_budget(run_dir: Path) -> None:
    """r6 F2: the limits bound a row to the budget, so a wide row of large
    values is refused by SQLite before it is built."""
    import issue_orchestrator.execution.improver_toolbox as toolbox_module

    assert toolbox_module.MAX_SQL_VALUE_BYTES * toolbox_module.MAX_SQL_COLUMNS <= toolbox_module.MAX_SQL_FETCH_BYTES
    wide = ", ".join(["zeroblob(900000)"] * 16)
    with pytest.raises(ToolboxRefusal, match="too big"):
        _toolbox(run_dir).sql_query("timeline.sqlite", f"SELECT {wide}")
    too_many = ", ".join(["1"] * (toolbox_module.MAX_SQL_COLUMNS + 1))
    with pytest.raises(ToolboxRefusal):
        _toolbox(run_dir).sql_query("timeline.sqlite", f"SELECT {too_many}")


def test_an_oversized_github_answer_is_refused_with_a_hint(run_dir: Path) -> None:
    from issue_orchestrator.ports.improver_toolbox import AuditedReadTooLarge

    class TooLarge:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def get(self, read: GitHubRead, *, max_bytes: int) -> Any:
            self.limits.append(max_bytes)
            raise AuditedReadTooLarge("GitHub GET returned more than the limit")

    reads = TooLarge()
    with pytest.raises(ToolboxRefusal, match="narrow the request"):
        _toolbox(run_dir, reads).github_get("repos/porchpin/porchpin/git/blobs/abc")  # type: ignore[arg-type]

    import issue_orchestrator.execution.improver_toolbox as toolbox_module

    assert reads.limits == [toolbox_module.MAX_GITHUB_BYTES]


def test_a_served_answer_is_citable_by_its_call_and_a_refusal_never_is(run_dir: Path) -> None:
    """#8001: each answer the agent is served begins ``[toolbox call N]`` and
    is kept as ``toolbox-answers/N.txt``; a design finding cites it by N."""
    from issue_orchestrator.domain.improver_citations import CitationCheck
    from issue_orchestrator.execution.improver_citations import RunDirCitations

    with serve_toolbox(_toolbox(run_dir, FakeReads())) as endpoint:
        refused = _call(endpoint.url, endpoint.token, "git", {"args": ["fetch"]})
        ok = _call(endpoint.url, endpoint.token, "sql_query", {"database": "timeline.sqlite", "sql": "SELECT event FROM timeline"})

    assert refused[0] is True and ok[0] is False
    assert ok[1].startswith("[toolbox call 2]\n")
    calls = [json.loads(line) for line in (run_dir / CALL_LOG).read_text().splitlines()]
    assert [(c["call"], c["outcome"].split(":")[0]) for c in calls] == [(1, "refused"), (2, "ok")]
    citations = RunDirCitations(run_dir)
    assert citations.quote_in_answer(2, '[["review.skipped"], ["rework.queued"]]') is CitationCheck.FOUND
    assert citations.quote_in_answer(1, "is not a read; allowed") is CitationCheck.NO_SUCH_SOURCE
