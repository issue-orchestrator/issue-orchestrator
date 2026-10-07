"""The run directory's citable evidence, as the validator checks it (#8001).

:class:`RunDirCitations` implements
:class:`~..domain.improver_citations.CitationIndex` over one run directory:

* a FILE citation must resolve, after every symlink, inside the run's
  evidence roots: ``improver-data/`` (the staged bundle) or ``toolbox/``
  (the staged toolbox). The prompt file, the agent's own workspace and
  anything outside the run are never evidence;
* a TOOL citation names a toolbox call whose answer the orchestrator
  recorded in ``toolbox-answers/<call>.txt`` (:mod:`.improver_toolbox`), and
  only an answer that is DATA counts (r1 F1): a ``github_get`` answer is
  GitHub's; a ``sql_query`` value counts only if it is inside a LIVE text
  value of the queried store copy (``SELECT 'any claim'`` or ``char(...)``
  produce text that is not, and a deleted row's leftover bytes are not a
  value); a ``git`` answer never counts, since ``--format`` and its
  escapes let the request write it (the clone's files are cited instead).

A file is read as a stream up to the cited line (engine logs run to hundreds
of megabytes), never whole.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any

from ..contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ..contracts.improver_toolbox import (
    TOOLBOX_ANSWERS_DIRNAME,
    TOOLBOX_CALL_LOG,
    TOOLBOX_DIRNAME,
    TOOLBOX_STATE_DIRNAME,
)
from ..domain.improver_citations import LINE_SLACK, CitationCheck, quoted_in


class RunDirCitations:
    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir.resolve()
        self._roots = tuple(self._run_dir / name for name in (IMPROVER_DATA_DIRNAME, TOOLBOX_DIRNAME))

    def quote_at(self, path: str, line: int, quote: str) -> CitationCheck:
        target = (self._run_dir / path).resolve()
        if not any(target.is_relative_to(root) for root in self._roots):
            return CitationCheck.OUTSIDE_EVIDENCE
        if not target.is_file():
            return CitationCheck.NO_SUCH_SOURCE
        window = _lines_around(target, line)
        if window is None:
            return CitationCheck.NO_SUCH_SOURCE
        return CitationCheck.FOUND if quoted_in(quote, "\n".join(window)) else CitationCheck.QUOTE_NOT_FOUND

    def quote_in_answer(self, call: int, quote: str) -> CitationCheck:
        answer = self._run_dir / TOOLBOX_ANSWERS_DIRNAME / f"{call}.txt"
        request = self._calls().get(call)
        if not answer.is_file() or request is None:
            return CitationCheck.NO_SUCH_SOURCE
        if not quoted_in(quote, answer.read_text(encoding="utf-8", errors="replace")):
            return CitationCheck.QUOTE_NOT_FOUND
        tool, arguments = request
        if tool == "github_get":
            return CitationCheck.FOUND
        if tool == "sql_query":
            return self._stored(str(arguments.get("database", "")), quote)
        return CitationCheck.NOT_EVIDENCE

    def _stored(self, database: str, quote: str) -> CitationCheck:
        """Whether ``quote`` (as quoted, or JSON-unescaped as the answer
        showed it) is inside one LIVE text value of the staged store copy:
        a value a query can return from a row, never bytes a deleted row
        left in a free page (r2 F1)."""
        state = (self._run_dir / TOOLBOX_DIRNAME / TOOLBOX_STATE_DIRNAME).resolve()
        store = (state / database).resolve()
        if store.parent != state or not store.is_file() or store.stat().st_size == 0:
            return CitationCheck.NOT_EVIDENCE
        needles = {quote}
        with contextlib.suppress(json.JSONDecodeError):
            needles.add(json.loads(f'"{quote}"'))
        # The dump holds every LIVE row, one INSERT per row; a deleted row's
        # leftover bytes are not dumped. The quote must lie inside ONE text
        # value of a row, never across values or the dump's own SQL (r3 F1).
        with closing(sqlite3.connect(f"{store.as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
            # The schema is stored data too: each object's live ``sql`` value.
            for (ddl,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
                if any(needle in ddl for needle in needles):
                    return CitationCheck.FOUND
            for statement in conn.iterdump():
                values = _text_values(statement)
                if values and any(needle in value for value in values for needle in needles):
                    return CitationCheck.FOUND
        return CitationCheck.NOT_EVIDENCE

    def _calls(self) -> dict[int, tuple[str, dict[str, Any]]]:
        """Each logged call's tool and arguments, by id (toolbox-calls.jsonl)."""
        log = self._run_dir / TOOLBOX_CALL_LOG
        if not log.is_file():
            return {}
        calls: dict[int, tuple[str, dict[str, Any]]] = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            calls[int(entry["call"])] = (str(entry["tool"]), dict(entry.get("arguments") or {}))
        return calls


def _text_values(statement: str) -> list[str] | None:
    """The text values of one ``iterdump`` row, ``INSERT INTO "t" VALUES(...);``,
    each unescaped; None if the line is not such a row. Only string literals
    are values here: SQL syntax, numbers, NULL and X'..' blobs are not."""
    prefix = 'INSERT INTO "'
    if not statement.startswith(prefix):
        return None
    i = len(prefix)
    while True:  # the table name: a quoted identifier, "" escaping a quote
        end = statement.find('"', i)
        if end < 0:
            return None
        if statement.startswith('""', end):
            i = end + 2
            continue
        break
    i = end + 1
    if not statement.startswith(" VALUES(", i):
        return None
    i += len(" VALUES(")
    values: list[str] = []
    while i < len(statement):
        char = statement[i]
        if char == "'" and statement[i - 1] != "X":
            text, i = _literal(statement, i + 1)
            values.append(text)
            continue
        if char == "'":  # X'..' blob: skip its hex
            i = statement.index("'", i + 1) + 1
            continue
        i += 1
    return values


def _literal(statement: str, i: int) -> tuple[str, int]:
    """The SQL string literal starting after its opening quote at ``i``."""
    parts: list[str] = []
    while True:
        end = statement.index("'", i)
        parts.append(statement[i:end])
        if statement.startswith("''", end):
            parts.append("'")
            i = end + 2
            continue
        return "".join(parts), end + 1


def _lines_around(path: Path, line: int) -> list[str] | None:
    """Lines ``line - LINE_SLACK`` to ``line + LINE_SLACK`` (1-based), or
    None if the file has fewer than ``line`` lines."""
    first = max(1, line - LINE_SLACK)
    last = line + LINE_SLACK
    window: deque[str] = deque()
    seen = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for seen, text in enumerate(handle, start=1):
            if seen >= first:
                window.append(text)
            if seen >= last:
                break
    return None if seen < line else list(window)


__all__ = ["RunDirCitations"]
