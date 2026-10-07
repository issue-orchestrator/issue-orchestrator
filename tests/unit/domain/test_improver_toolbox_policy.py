"""The empowered improver's toolbox lets through reads of the audited repository only (#8001)."""

from __future__ import annotations

import sqlite3

import pytest

from issue_orchestrator.domain.improver_toolbox_policy import (
    AuditedRepoReadPolicy,
    GitReadPolicy,
    ToolboxRefusal,
    sqlite_action_allowed,
)

POLICY = AuditedRepoReadPolicy("porchpin/porchpin")


@pytest.mark.parametrize(
    ("path", "params", "expected"),
    [
        ("repos/porchpin/porchpin/issues/450", None, "/repos/porchpin/porchpin/issues/450"),
        ("/repos/porchpin/porchpin/issues/450/timeline", {"per_page": 100}, "/repos/porchpin/porchpin/issues/450/timeline"),
        ("repos/Porchpin/PORCHPIN/pulls/479/reviews", None, "/repos/Porchpin/PORCHPIN/pulls/479/reviews"),
        ("repos/porchpin/porchpin", None, "/repos/porchpin/porchpin"),
        ("search/issues", {"q": "repo:porchpin/porchpin is:open label:needs-human"}, "/search/issues"),
        ("search/issues", {"q": 'is:pr repo:"porchpin/porchpin"'}, "/search/issues"),
        ("search/issues", {"q": 'repo:porchpin/porchpin label:"needs human" "or" in:title'}, "/search/issues"),
        ("search/commits", {"q": "repo:porchpin/porchpin fix"}, "/search/commits"),
    ],
)
def test_reads_of_the_audited_repository_pass(path: str, params: dict | None, expected: str) -> None:
    assert POLICY.check(path, params).path == expected


@pytest.mark.parametrize(
    ("path", "params"),
    [
        # Another repository: io's own issues would leak the answer key.
        ("repos/issue-orchestrator/issue-orchestrator/issues/8001", None),
        ("repos/porchpin/porchpin-other/issues/1", None),
        ("repos/porchpin", None),
        # Escaping the repository's path.
        ("repos/porchpin/porchpin/../../issue-orchestrator/issue-orchestrator/issues/1", None),
        ("repos/porchpin/porchpin/%2e%2e/%2e%2e/orgs/x", None),
        ("repos/porchpin/porchpin/issues/1?per_page=100", None),
        ("repos/porchpin/porchpin\\..\\x", None),
        ("https://api.github.com/repos/issue-orchestrator/issue-orchestrator/issues", None),
        ("//evil.example/repos/porchpin/porchpin", None),
        # Other endpoints.
        ("graphql", None),
        ("user", None),
        ("orgs/porchpin/repos", None),
        ("search/code", {"q": "repo:porchpin/porchpin token"}),
        # Searches that reach beyond the repository.
        ("search/issues", {"q": "is:open label:bug"}),
        ("search/issues", {"q": "repo:issue-orchestrator/issue-orchestrator improver"}),
        ("search/issues", {"q": "repo:porchpin/porchpin repo:issue-orchestrator/issue-orchestrator x"}),
        ("search/issues", {"q": "repo:porchpin/porchpin org:issue-orchestrator"}),
        ("search/issues", {"q": "repo:porchpin/porchpin user:BruceBGordon"}),
        ("search/issues", {"q": "repo:porchpin/porchpin -repo:porchpin/porchpin"}),
        # r1 F1: one scoped side of OR does not scope the other; a phrase is not a scope.
        ("search/issues", {"q": "repo:porchpin/porchpin OR is:issue"}),
        ("search/issues", {"q": "repo:porchpin/porchpin or is:issue"}),
        ("search/issues", {"q": "repo:porchpin/porchpin AND is:issue"}),
        ("search/issues", {"q": "repo:porchpin/porchpin NOT is:pr"}),
        ("search/issues", {"q": "(repo:porchpin/porchpin is:issue) OR label:x"}),
        ("search/issues", {"q": '"repo:porchpin/porchpin" improver'}),
        ("search/issues", {"q": '"needs human repo:porchpin/porchpin"'}),
        ("search/issues", {"q": 'repo:porchpin/porchpin "unbalanced'}),
        ("search/issues", {"q": "REPO:issue-orchestrator/issue-orchestrator"}),
        # Parameters are scalars.
        ("repos/porchpin/porchpin/issues", {"labels": ["a", "b"]}),
        ("repos/porchpin/porchpin/issues", {"q x": "1"}),
    ],
)
def test_everything_else_is_refused(path: str, params: dict | None) -> None:
    with pytest.raises(ToolboxRefusal):
        POLICY.check(path, params)


def test_a_policy_needs_an_owner_and_repo() -> None:
    for bad in ("porchpin", "a/b/c", "../x", ""):
        with pytest.raises(ValueError):
            AuditedRepoReadPolicy(bad)


GIT = GitReadPolicy()


@pytest.mark.parametrize(
    "args",
    [
        ["log", "--oneline", "-30", "--", "src"],
        ["log", "-S", "needs-human", "--all"],
        ["log", "-Sneeds-human"],
        ["log", "-n5"],
        ["show", "abc123", "--stat"],
        ["blame", "-L", "10,20", "README.md"],
        ["grep", "-n", "milestone", "HEAD"],
        ["diff", "HEAD~3", "HEAD", "--", "-O"],  # after --, a pathspec
        ["for-each-ref", "refs/remotes"],
    ],
)
def test_history_reads_pass(args: list[str]) -> None:
    assert GIT.check(args) == tuple(args)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["fetch"],
        ["push", "origin"],
        ["checkout", "main"],
        ["config", "--list"],
        ["-c", "core.pager=sh", "log"],
        # Writes a file.
        ["log", "--output=/tmp/x"],
        ["log", "--output", "/tmp/x"],
        ["diff", "--outp=/tmp/x"],  # Git expands a unique prefix
        # Reads a file outside the repository.
        ["diff", "--no-index", "/etc/hosts", "/dev/null"],
        ["diff", "--no-ind", "/etc/hosts", "/dev/null"],
        ["blame", "--contents", "/etc/hosts", "README.md"],
        ["blame", "--conte", "/etc/hosts", "README.md"],
        ["blame", "-S", "/etc/hosts", "README.md"],
        ["blame", "--ignore-revs-file", "/etc/hosts", "README.md"],
        ["grep", "-f", "/etc/hosts"],
        ["grep", "-nf/etc/hosts"],
        ["grep", "--file=/etc/hosts"],
        ["log", "--pathspec-from-file=/etc/hosts"],
        # Runs a program.
        ["grep", "-O", "x"],
        ["grep", "-nOless", "x"],
        # A refused letter anywhere in a bundle: its meaning is the subcommand's.
        ["log", "-Sfoo"],
        ["grep", "--open-files-in-pager=sh", "x"],
        ["diff", "--ext-diff"],
        ["log", "--textconv", "-p"],
        ["log", "--git-dir=/Users/someone/dev/issue-orchestrator/.git"],
    ],
)
def test_writes_outside_reads_and_programs_are_refused(args: list[str]) -> None:
    with pytest.raises(ToolboxRefusal):
        GIT.check(args)


@pytest.mark.parametrize(
    ("action", "arg1", "arg2", "allowed"),
    [
        (sqlite3.SQLITE_SELECT, None, None, True),
        (sqlite3.SQLITE_READ, "timeline", "id", True),
        (sqlite3.SQLITE_FUNCTION, None, "count", True),
        (sqlite3.SQLITE_PRAGMA, "table_info", "timeline", True),
        (sqlite3.SQLITE_ATTACH, "/Users/x/.claude/x.db", None, False),
        (sqlite3.SQLITE_INSERT, "timeline", None, False),
        (sqlite3.SQLITE_UPDATE, "timeline", "id", False),
        (sqlite3.SQLITE_DELETE, "timeline", None, False),
        (sqlite3.SQLITE_CREATE_TABLE, "x", None, False),
        (sqlite3.SQLITE_DROP_TABLE, "x", None, False),
        (sqlite3.SQLITE_PRAGMA, "journal_mode", "wal", False),
        (sqlite3.SQLITE_PRAGMA, "writable_schema", "1", False),
        (sqlite3.SQLITE_FUNCTION, None, "load_extension", False),
        (sqlite3.SQLITE_FUNCTION, None, "readfile", False),
        (sqlite3.SQLITE_FUNCTION, None, "writefile", False),
    ],
)
def test_the_sqlite_authorizer_admits_reads_only(action: int, arg1: str | None, arg2: str | None, allowed: bool) -> None:
    assert sqlite_action_allowed(action, arg1, arg2) is allowed
