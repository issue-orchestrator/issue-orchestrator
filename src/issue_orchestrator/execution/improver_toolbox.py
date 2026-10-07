"""The empowered improver's read-only tools, and the server that offers them (#8001).

:class:`ImproverToolbox` is each tool's behaviour, policy first
(:mod:`..domain.improver_toolbox_policy`), testable without a transport:

* ``github_get`` — a GitHub ``GET`` of the audited repository;
* ``sql_query`` — one read-only query over a staged store copy, opened
  ``immutable`` under an authorizer that admits only reads;
* ``git`` — a read-only Git command in the staged clone, with no user or
  system Git configuration (no alias, pager or external program).

:func:`serve_toolbox` offers them as an MCP server over HTTP on 127.0.0.1,
run by the ORCHESTRATOR, not by the agent: the GitHub credential stays in
this process, and the agent needs no shell. A per-run bearer token gates
every request. Each call is appended to ``toolbox-calls.jsonl`` in the run
directory, so a run shows how deep the agent dug.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts.improver_toolbox import TOOLBOX_DIRNAME, TOOLBOX_REPO_DIRNAME, TOOLBOX_STATE_DIRNAME
from ..domain.improver_toolbox_policy import (
    AuditedRepoReadPolicy,
    GitReadPolicy,
    ToolboxRefusal,
    sqlite_action_allowed,
)
from ..ports.improver_toolbox import TOOLBOX_SERVER_NAME, AuditedRepoReads, ToolboxEndpoint

#: Each answer is capped, so one call cannot flood the agent's context.
MAX_ANSWER_CHARS = 200_000
MAX_SQL_ROWS = 1000
MAX_CELL_CHARS = 4000
#: SQLite's own limits for a toolbox query, set before it runs: no value it
#: computes or returns may exceed MAX_SQL_VALUE_BYTES (``randomblob(1e9)``
#: fails instead of allocating), nor a row more than MAX_SQL_COLUMNS values.
MAX_SQL_VALUE_BYTES = 1_000_000
MAX_SQL_COLUMNS = 64
MAX_SQL_TEXT = 100_000
#: Rows are fetched one by one until their cells add up to this many bytes.
MAX_SQL_FETCH_BYTES = 4_000_000
SQL_SECONDS = 20.0
GIT_SECONDS = 60
CALL_LOG = "toolbox-calls.jsonl"


class ImproverToolbox:
    def __init__(
        self,
        *,
        run_dir: Path,
        audited_repo: str,
        github: AuditedRepoReads | None,
        hidden_issues: frozenset[int] = frozenset(),
    ) -> None:
        self._root = run_dir / TOOLBOX_DIRNAME
        self._state = self._root / TOOLBOX_STATE_DIRNAME
        self._repo = self._root / TOOLBOX_REPO_DIRNAME
        self._github_policy = AuditedRepoReadPolicy(audited_repo, hidden_issues=hidden_issues)
        self._hidden = hidden_issues
        self._audited_repo = audited_repo
        self._git_policy = GitReadPolicy()
        self._github = github
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("the improver toolbox needs git on PATH")
        self._git_binary = git
        self._log = run_dir / CALL_LOG
        self._log_lock = threading.Lock()

    def github_get(self, path: str, params: dict[str, Any] | None = None) -> str:
        read = self._github_policy.check(path, params)
        if self._github is None:
            raise ToolboxRefusal("GitHub reads are off for this run")
        answer = self._github.get(read)
        if read.path.startswith("/search/"):
            self._check_search_results(answer)
        if self._hidden and _mentions_issue(answer, self._hidden):
            raise ToolboxRefusal("the answer holds an issue this blind run may not see; refused")
        return _capped(json.dumps(answer, indent=1, default=str))

    def _check_search_results(self, answer: Any) -> None:
        """The second wall behind the query policy: a search answer holding
        any item that does not name the audited repository is refused whole."""
        items = answer.get("items") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            raise ToolboxRefusal("the search answer has no item list; refused")
        wanted = f"/repos/{self._audited_repo}".casefold()
        for item in items:
            repository = item.get("repository") if isinstance(item, dict) else None
            url = (
                item.get("repository_url")
                if isinstance(item, dict) and "repository_url" in item
                else repository.get("url") if isinstance(repository, dict) else None
            )
            if not isinstance(url, str) or not url.casefold().endswith(wanted):
                raise ToolboxRefusal(f"the search returned an item outside {self._audited_repo}; refused")

    def sql_query(self, database: str, sql: str) -> str:
        copy = self._database(database)
        deadline = time.monotonic() + SQL_SECONDS
        with closing(sqlite3.connect(f"{copy.as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQL_VALUE_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_SQL_COLUMNS)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_TEXT)
            conn.set_authorizer(_authorize)
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                cursor = conn.execute(sql)
                rows, truncated = _bounded_rows(cursor)
            except sqlite3.DatabaseError as error:
                raise ToolboxRefusal(f"query refused or failed: {error}") from error
            columns = [d[0] for d in cursor.description or ()]
        answer = {"columns": columns, "rows": rows, "truncated": truncated}
        return _capped(json.dumps(answer))

    def git(self, args: list[str]) -> str:
        checked = self._git_policy.check(args)
        if not (self._repo / ".git").is_dir():
            raise ToolboxRefusal("no clone of the audited repository was staged (see toolbox/toolbox.json)")
        done = _bounded_run(
            [self._git_binary, "--no-pager", "-C", str(self._repo), "-c", "core.fsmonitor=false", *checked],
            env=_git_environment(self._root),
            limit_bytes=MAX_ANSWER_CHARS,
            timeout_seconds=GIT_SECONDS,
        )
        if done.timed_out:
            raise ToolboxRefusal(f"git timed out after {GIT_SECONDS}s")
        text = done.stdout.decode("utf-8", errors="replace")
        if done.truncated:
            return text + f"\n... <truncated at {MAX_ANSWER_CHARS} bytes; narrow the request>"
        if done.returncode:
            raise ToolboxRefusal(f"git exited {done.returncode}: {done.stderr_tail}")
        return text

    def record(self, tool: str, arguments: dict[str, Any], outcome: str) -> None:
        line = json.dumps(
            {"at": datetime.now(UTC).isoformat(), "tool": tool, "arguments": arguments, "outcome": outcome},
            default=str,
        )
        with self._log_lock, self._log.open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    def _database(self, name: str) -> Path:
        if "/" in name or "\\" in name or name.startswith(".") or not name.endswith(".sqlite"):
            raise ToolboxRefusal(f"name a staged store, e.g. timeline.sqlite; refused {name!r}")
        copy = self._state / name
        if not copy.is_file():
            staged = sorted(p.name for p in self._state.glob("*.sqlite"))
            raise ToolboxRefusal(f"no staged store {name!r}; staged: {', '.join(staged) or 'none'}")
        return copy


def _bounded_rows(cursor: sqlite3.Cursor) -> tuple[list[list[object]], bool]:
    """At most MAX_SQL_ROWS rows and MAX_SQL_FETCH_BYTES of cells, fetched
    one row at a time so an answer never grows past its budget."""
    rows: list[list[object]] = []
    fetched = 0
    for row in cursor:
        size = sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in row)
        if len(rows) == MAX_SQL_ROWS or fetched + size > MAX_SQL_FETCH_BYTES:
            return rows, True
        fetched += size
        rows.append([_cell(v) for v in row])
    return rows, False


def _authorize(action: int, arg1: str | None, arg2: str | None, _db: str | None, _source: str | None) -> int:
    # SQLite reads the authorizer's answer as a code: 0 allows, 1 denies.
    return sqlite3.SQLITE_OK if sqlite_action_allowed(action, arg1, arg2) else sqlite3.SQLITE_DENY


def _mentions_issue(value: Any, numbers: frozenset[int]) -> bool:
    """Whether ``value`` holds, at any depth, an issue or pull request
    (an object with a ``number`` and a ``title``) numbered in ``numbers``."""
    if isinstance(value, dict):
        number = value.get("number")
        if isinstance(number, int) and number in numbers and "title" in value:
            return True
        return any(_mentions_issue(v, numbers) for v in value.values())
    if isinstance(value, list):
        return any(_mentions_issue(v, numbers) for v in value)
    return False


@dataclass(frozen=True)
class _Bounded:
    returncode: int | None
    stdout: bytes
    truncated: bool
    timed_out: bool
    stderr_tail: str


def _bounded_run(argv: list[str], *, env: dict[str, str], limit_bytes: int, timeout_seconds: int) -> _Bounded:
    """Run ``argv``, keeping at most ``limit_bytes`` of its output: past the
    limit the process group is killed, so a huge blob is never held whole
    (``git show HEAD:big.bin``). stderr goes to a file; its tail is kept."""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL, env=env, start_new_session=True
        )
        fired = threading.Event()

        def stop() -> None:
            fired.set()
            _kill_group(process)

        timer = threading.Timer(timeout_seconds, stop)
        timer.start()
        kept = bytearray()
        truncated = False
        stdout = process.stdout
        if stdout is None:
            raise RuntimeError("the child process has no stdout pipe")
        try:
            with stdout:
                while chunk := os.read(stdout.fileno(), 65536):
                    room = limit_bytes - len(kept)
                    kept += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                        _kill_group(process)
                        break
        finally:
            timer.cancel()
            returncode = process.wait()
        errors.seek(0)
        tail = errors.read()[-4000:].decode("utf-8", errors="replace").strip()[-2000:]
    return _Bounded(returncode, bytes(kept), truncated, fired.is_set(), tail)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _git_environment(home: Path) -> dict[str, str]:
    """No user or system configuration: no alias, pager, hook or external diff."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS": "1",
        "LANG": "C.UTF-8",
    }


def _cell(value: object) -> object:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + f"... <{len(value) - MAX_CELL_CHARS} more chars>"
    return value


def _capped(text: str) -> str:
    if len(text) <= MAX_ANSWER_CHARS:
        return text
    return text[:MAX_ANSWER_CHARS] + f"\n... <truncated: {len(text) - MAX_ANSWER_CHARS} more chars; narrow the request>"


def build_mcp_app(toolbox: ImproverToolbox, token: str) -> Any:
    """The toolbox's MCP server as an ASGI app that refuses any request
    without the run's bearer token."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(TOOLBOX_SERVER_NAME, stateless_http=True, json_response=True, log_level="WARNING")
    for tool in _tools(toolbox):
        mcp.add_tool(tool)
    return _bearer_guarded(mcp.streamable_http_app(), token)


def _tools(toolbox: ImproverToolbox) -> tuple[Callable[..., str], ...]:
    """The tools as the agent sees them: their names, signatures and docs."""

    def github_get(path: str, params: dict[str, str | int | bool] | None = None) -> str:
        """GET one GitHub REST API path of the AUDITED repository, e.g.
        repos/OWNER/REPO/issues/450, repos/OWNER/REPO/issues/450/timeline,
        repos/OWNER/REPO/pulls/479/reviews, repos/OWNER/REPO/actions/runs, or
        search/issues with params q="repo:OWNER/REPO ...". Query parameters
        (per_page, page, state, since, ...) go in params. Read-only."""
        return _call(toolbox, "github_get", {"path": path, "params": params}, lambda: toolbox.github_get(path, params))

    def sql_query(database: str, sql: str) -> str:
        """Run one read-only SQL query on a byte copy of an engine store in
        toolbox/state/ (e.g. database="timeline.sqlite"). SELECT and schema
        pragmas only; at most 1000 rows. List tables with
        SELECT name, sql FROM sqlite_master."""
        return _call(toolbox, "sql_query", {"database": database, "sql": sql}, lambda: toolbox.sql_query(database, sql))

    def git(args: list[str]) -> str:
        """Run a read-only git command in the clone of the audited repository
        (toolbox/repo), e.g. ["log", "--oneline", "-20", "--", "src"] or
        ["show", "abc123", "--stat"]. Read subcommands only."""
        return _call(toolbox, "git", {"args": args}, lambda: toolbox.git(args))

    return (github_get, sql_query, git)


def _call(toolbox: ImproverToolbox, tool: str, arguments: dict[str, Any], run: Callable[[], str]) -> str:
    """Run one tool call, log it, and turn a refusal or failure into the
    agent's tool error."""
    from mcp.server.fastmcp.exceptions import ToolError

    try:
        answer = run()
    except ToolboxRefusal as refusal:
        toolbox.record(tool, arguments, f"refused: {refusal}")
        raise ToolError(str(refusal)) from refusal
    except Exception as error:  # a failed read is the agent's to know about
        toolbox.record(tool, arguments, f"failed: {type(error).__name__}: {error}")
        raise ToolError(f"{type(error).__name__}: {error}") from error
    toolbox.record(tool, arguments, f"ok: {len(answer)} chars")
    return answer


def _bearer_guarded(app: Any, token: str) -> Any:
    expected = f"Bearer {token}".encode()

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            presented = dict(scope.get("headers") or ()).get(b"authorization", b"")
            if not secrets.compare_digest(presented, expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    return guarded


@contextmanager
def serve_toolbox(toolbox: ImproverToolbox) -> Iterator[ToolboxEndpoint]:
    """Serve ``toolbox`` on 127.0.0.1 for the duration of the block."""
    import uvicorn

    token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(build_mcp_app(toolbox, token), log_level="warning", lifespan="on", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="improver-toolbox", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            sock.close()
            raise RuntimeError("the improver toolbox server did not start")
        time.sleep(0.05)
    try:
        yield ToolboxEndpoint(url=f"http://127.0.0.1:{port}/mcp", token=token)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


__all__ = ["CALL_LOG", "ImproverToolbox", "build_mcp_app", "serve_toolbox"]
