"""What the empowered improver's read-only toolbox lets through (#8001).

The improver investigates with three tools beyond reading its run directory:
GitHub reads, SQL over byte copies of the engine's stores, and Git history of
a clone of the audited repository. Each tool's request is checked here,
before anything runs, by a pure policy:

* :class:`AuditedRepoReadPolicy` — GitHub ``GET`` of the AUDITED repository
  only (the 2026-10-04 tournament's ``ppgh`` guard, productized). Reading
  another repository would leak other people's conclusions into the
  improver's (io's own issues track what the improver should find), and a
  write is never a read.
* :class:`GitReadPolicy` — the Git subcommands that only read history, minus
  every option that writes a file (``--output``) or reads one outside the
  repository (``--no-index``, ``--contents``, pattern and pathspec files).
* :func:`sqlite_action_allowed` — an SQLite authorizer: SELECTs and the
  schema-reading pragmas, never a write, an ``ATTACH`` (which reads, and
  creates, any file) or an extension.

A refusal is a :class:`ToolboxRefusal`, whose message goes back to the agent.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class ToolboxRefusal(ValueError):
    """The request is outside the toolbox; the message says why."""


_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PARAM_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_\[\]]*$")
#: Search endpoints scoped by a ``repo:`` qualifier in ``q``.
_SEARCHES = frozenset({"issues", "commits"})
#: One search term: a qualifier (its value may be quoted), a quoted phrase,
#: or a bare word. An unbalanced quote matches none of them.
_SEARCH_TERM = re.compile(r'(-?[A-Za-z_-]+:"[^"]*")|("[^"]*")|([^\s"]+)')
#: A qualifier that scopes a search by owner or repository.
_SCOPE_QUALIFIER = re.compile(r'(?i)^(-?)(repo|org|user|owner):"?([^"]*)"?$')
#: GitHub's boolean search operators: ``a OR b`` matches either side, so one
#: scoped side would not scope the other.
_SEARCH_OPERATORS = frozenset({"or", "and", "not"})


@dataclass(frozen=True)
class GitHubRead:
    """One allowed ``GET``: an API path (leading ``/``) and its query."""

    path: str
    params: Mapping[str, str]


class AuditedRepoReadPolicy:
    """``GET`` of ``repos/<audited>/...`` and of an issue or commit search
    scoped to the audited repository by its own ``repo:`` qualifier."""

    def __init__(self, repo: str) -> None:
        owner, _, name = repo.partition("/")
        if (
            "/" in name
            or any(part in ("", ".", "..") or not _SEGMENT.match(part) for part in (owner, name))
        ):
            raise ValueError(f"not an owner/repo: {repo!r}")
        self.repo = repo

    def check(self, path: str, params: Mapping[str, object] | None = None) -> GitHubRead:
        query = _params(params or {})
        segments = _segments(path)
        if segments[0] == "repos":
            if len(segments) < 3 or f"{segments[1]}/{segments[2]}".lower() != self.repo.lower():
                raise ToolboxRefusal(f"only {self.repo} may be read; refused {path!r}")
            return GitHubRead("/" + "/".join(segments), query)
        if segments[0] == "search" and len(segments) == 2 and segments[1] in _SEARCHES:
            self._check_search(query.get("q", ""))
            return GitHubRead("/" + "/".join(segments), query)
        raise ToolboxRefusal(
            f"only repos/{self.repo}/... and search/issues or search/commits are readable; refused {path!r}"
        )

    def _check_search(self, q: str) -> None:
        """Every result must come from the audited repository: the query is
        scoped by ``repo:<audited>`` and by no other scope, and has no boolean
        operator or grouping that could match beside the scope. A ``repo:``
        inside a quoted phrase is text, not a scope."""
        refused = ToolboxRefusal(
            f"a search is scoped with repo:{self.repo} in q, names no other scope, and uses no"
            f" OR/AND/NOT or parentheses; refused q={q!r}"
        )
        terms = _search_terms(q)
        if terms is None:
            raise refused
        scoped = False
        for qualifier, phrase, word in terms:
            if phrase:
                continue
            if word and (word.casefold() in _SEARCH_OPERATORS or "(" in word or ")" in word):
                raise refused
            scope = _SCOPE_QUALIFIER.match(qualifier or word)
            if scope is None:
                continue
            negated, kind, value = scope.groups()
            if negated or kind.casefold() != "repo" or value.casefold() != self.repo.casefold():
                raise refused
            scoped = True
        if not scoped:
            raise refused


def _search_terms(q: str) -> list[tuple[str, str, str]] | None:
    """``q``'s terms, or ``None`` if a quote is unbalanced (GitHub's reading
    of such a query is not this policy's)."""
    terms = []
    position = 0
    for match in _SEARCH_TERM.finditer(q):
        if q[position:match.start()].strip():
            return None
        terms.append((match.group(1) or "", match.group(2) or "", match.group(3) or ""))
        position = match.end()
    if q[position:].strip() or q.count('"') % 2:
        return None
    return terms


def _segments(path: str) -> list[str]:
    stripped = path.strip()
    if "://" in stripped or any(c in stripped for c in "?#%\\ \t\n"):
        raise ToolboxRefusal(f"a path is an API path like repos/owner/repo/issues/1, without a query; refused {path!r}")
    segments = stripped.strip("/").split("/")
    if not segments or any(s in ("", ".", "..") or not _SEGMENT.match(s) for s in segments):
        raise ToolboxRefusal(f"malformed API path {path!r}")
    return segments


def _params(params: Mapping[str, object]) -> dict[str, str]:
    query: dict[str, str] = {}
    for key, value in params.items():
        if not _PARAM_KEY.match(key) or isinstance(value, (dict, list, tuple, set)) or value is None:
            raise ToolboxRefusal(f"query parameters are name=scalar; refused {key!r}")
        query[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return query


#: Git subcommands that only read the repository.
GIT_READ_SUBCOMMANDS = frozenset(
    {
        "log", "show", "diff", "blame", "grep", "ls-tree", "ls-files", "rev-parse", "rev-list",
        "cat-file", "shortlog", "describe", "merge-base", "name-rev", "for-each-ref", "show-ref",
    }
)
#: Options that write a file, run a program, or read a file outside the
#: repository. Matched as the whole option or its ``--opt=`` form.
_GIT_DENIED_OPTIONS = frozenset(
    {
        "--output", "--no-index", "--contents", "--ext-diff", "--textconv", "--open-files-in-pager",
        "-O", "--file", "-f", "--exclude-from", "-X", "--exec", "--upload-pack", "--config",
        "--paginate", "--git-dir", "--work-tree", "--exclude-per-directory",
        "--pathspec-from-file", "--ignore-revs-file",
    }
)
_GIT_DENIED_LONG = frozenset(o for o in _GIT_DENIED_OPTIONS if o.startswith("--"))
#: Per-subcommand options with a different meaning elsewhere: ``blame -S``
#: reads a revisions file (``log -S`` is the pickaxe and stays allowed).
_GIT_DENIED_BY_SUBCOMMAND = {"blame": frozenset({"-S"})}

class GitReadPolicy:
    def check(self, args: Sequence[str]) -> tuple[str, ...]:
        if not args:
            raise ToolboxRefusal("name a git subcommand, e.g. log")
        subcommand, *rest = args
        if subcommand not in GIT_READ_SUBCOMMANDS:
            raise ToolboxRefusal(
                f"git {subcommand!r} is not a read; allowed: {', '.join(sorted(GIT_READ_SUBCOMMANDS))}"
            )
        denied = _GIT_DENIED_OPTIONS | _GIT_DENIED_BY_SUBCOMMAND.get(subcommand, frozenset())
        for arg in rest:
            if "\0" in arg:
                raise ToolboxRefusal("a git argument holds a NUL byte")
            if arg == "--":
                break  # pathspecs follow
            if _git_option_denied(arg, denied):
                raise ToolboxRefusal(
                    f"git option {arg!r} writes, runs a program or reads outside the repository"
                    " (pass an option's value as its own argument, e.g. -S text)"
                )
        return (subcommand, *rest)


def _git_option_denied(arg: str, denied: frozenset[str]) -> bool:
    if arg.startswith("--"):
        option = arg.split("=", 1)[0]
        # Git accepts a unique prefix of a long option (``--conte`` is
        # ``blame --contents``), so an abbreviation of a denied one is denied.
        return (
            option in denied
            or option.endswith("-file")
            or (len(option) >= 4 and any(d.startswith(option) for d in _GIT_DENIED_LONG))
        )
    if not arg.startswith("-") or len(arg) < 2:
        return False
    # Git bundles short options (``grep -nfFILE`` is -n -f FILE), and which
    # letters take an attached value differs by subcommand, so a bundle with
    # a denied letter ANYWHERE is refused. A value that happens to hold one
    # (``log -Sfoo``) can be passed separately (``-S foo``).
    return any(f"-{letter}" in denied for letter in arg[1:])


#: Pragmas that only read the schema.
_READ_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo", "foreign_key_list"}
)
#: Functions an SQL query may not call.
_DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


def sqlite_action_allowed(action: int, arg1: str | None, arg2: str | None) -> bool:
    """The authorizer of a toolbox SQL query (``sqlite3.Connection.set_authorizer``)."""
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return True
    if action == sqlite3.SQLITE_FUNCTION:
        return (arg2 or "").lower() not in _DENIED_FUNCTIONS
    if action == sqlite3.SQLITE_PRAGMA:
        return (arg1 or "").lower() in _READ_PRAGMAS
    return False


__all__ = [
    "GIT_READ_SUBCOMMANDS",
    "AuditedRepoReadPolicy",
    "GitHubRead",
    "GitReadPolicy",
    "ToolboxRefusal",
    "sqlite_action_allowed",
]
