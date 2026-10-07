"""The run directory's citable evidence, as the validator checks it (#8001).

:class:`RunDirCitations` implements
:class:`~..domain.improver_citations.CitationIndex` over one run directory:

* a FILE citation must resolve, after every symlink, inside the run's
  evidence roots: ``improver-data/`` (the staged bundle) or ``toolbox/``
  (the staged toolbox). The prompt file, the agent's own workspace and
  anything outside the run are never evidence;
* a TOOL citation names a toolbox call whose answer the orchestrator
  recorded in ``toolbox-answers/<call>.txt`` (:mod:`.improver_toolbox`).

A file is read as a stream up to the cited line (engine logs run to hundreds
of megabytes), never whole.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from ..contracts.improver_inputs import IMPROVER_DATA_DIRNAME
from ..contracts.improver_toolbox import TOOLBOX_ANSWERS_DIRNAME, TOOLBOX_DIRNAME
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
        if not answer.is_file():
            return CitationCheck.NO_SUCH_SOURCE
        text = answer.read_text(encoding="utf-8", errors="replace")
        return CitationCheck.FOUND if quoted_in(quote, text) else CitationCheck.QUOTE_NOT_FOUND


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
