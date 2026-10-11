"""A cited file's lines are numbered as the agent's tools number them (#8001).

The first real porchpin run cited ``toolbox/logs/orchestrator.log:2285434``,
which is right by ``grep -n``. The log holds lone ``\\r`` from PTY output,
and a validator splitting by Python's universal-newline rules saw an
unrelated line there and rejected the citation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.improver_citations import LINE_SLACK, CitationCheck
from issue_orchestrator.execution.improver_citations import RunDirCitations

QUOTE = "Can't trigger event needs_human from state pr_pending!"
#: Everything str.splitlines (or universal newlines) breaks on, but grep -n does not.
NOT_LINE_BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("separator", NOT_LINE_BREAKS, ids=[f"U+{ord(c):04X}" for c in NOT_LINE_BREAKS])
def test_a_quote_is_found_on_the_line_grep_numbers_it(tmp_path: Path, separator: str) -> None:
    log = tmp_path / "toolbox" / "logs" / "orchestrator.log"
    log.parent.mkdir(parents=True)
    # Many lines, each holding the separator, push the quote far past what a
    # separator-splitting reader would count (and beyond the line slack).
    filler = [f"progress {n}{separator}redraw {n}" for n in range(LINE_SLACK * 4)]
    log.write_bytes(("\n".join([*filler, QUOTE, "after"]) + "\n").encode("utf-8"))
    line = len(filler) + 1
    assert log.read_bytes().split(b"\n")[line - 1].decode() == QUOTE  # as grep -n counts

    citations = RunDirCitations(tmp_path)

    assert citations.quote_at("toolbox/logs/orchestrator.log", line, QUOTE) is CitationCheck.FOUND
    assert citations.quote_at("toolbox/logs/orchestrator.log", 1, QUOTE) is CitationCheck.QUOTE_NOT_FOUND


def test_a_line_past_the_last_newline_counted_line_does_not_exist(tmp_path: Path) -> None:
    log = tmp_path / "toolbox" / "logs" / "orchestrator.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"one\rtwo\rthree\n")

    assert RunDirCitations(tmp_path).quote_at("toolbox/logs/orchestrator.log", 2, "two") is CitationCheck.NO_SUCH_SOURCE
