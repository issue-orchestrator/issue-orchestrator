"""How the validator checks a design finding's citations (#8001).

A design finding cites what the improver READ: a line of a staged file
(``improver-data/`` or ``toolbox/``) or an answer the toolbox served it. The
validator never trusts a citation: :class:`CitationIndex` looks it up in the
run directory, and the quote must be there.

Matching ignores differences in whitespace only (a log line's columns, a
JSON indent), and a file citation may be up to :data:`LINE_SLACK` lines off,
because agents count lines inexactly; the quote's words must be verbatim.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol

#: How far a file citation's quote may sit from its cited line.
LINE_SLACK = 2
#: A quote's least length once its whitespace is collapsed: long enough to be
#: found, not matched by chance (r1 F2: padding does not count).
MIN_QUOTE_CHARS = 12


class CitationCheck(StrEnum):
    FOUND = "found"
    #: The source exists, the quote is not in it (or not near the line).
    QUOTE_NOT_FOUND = "quote_not_found"
    #: No such staged file, line or toolbox answer.
    NO_SUCH_SOURCE = "no_such_source"
    #: The path resolves outside the run's evidence (``..``, a symlink out).
    OUTSIDE_EVIDENCE = "outside_evidence"
    #: A toolbox answer whose text the agent's own request can shape (a git
    #: ``--format``, a SQL literal), or a SQL value not in the store itself.
    NOT_EVIDENCE = "not_evidence"
    #: Shorter than :data:`MIN_QUOTE_CHARS` once whitespace is collapsed.
    TOO_SHORT = "too_short"


class CitationIndex(Protocol):
    def quote_at(self, path: str, line: int, quote: str) -> CitationCheck:
        """Whether ``quote`` is in run-dir file ``path`` within
        :data:`LINE_SLACK` lines of ``line``."""
        ...

    def quote_in_answer(self, call: int, quote: str) -> CitationCheck:
        """Whether ``quote`` is in the answer of toolbox call ``call`` AND is
        data rather than text the agent's request put there: a GitHub answer
        is GitHub's; a SQL answer counts only for a value present, byte for
        byte, in the queried store copy; a git answer never counts (its
        format is the request's; cite the clone's file instead)."""
        ...


_SPACE = re.compile(r"\s+")


def normalized(text: str) -> str:
    """``text`` with every whitespace run one space, and no edge whitespace."""
    return _SPACE.sub(" ", text).strip()


def quoted_in(quote: str, text: str) -> bool:
    return normalized(quote) in normalized(text)


__all__ = ["LINE_SLACK", "CitationCheck", "CitationIndex", "normalized", "quoted_in"]
