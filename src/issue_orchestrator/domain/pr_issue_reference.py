"""How an orchestrator PR body names its issue, and what that name promises.

An agent PR names its issue on the first line of its body in one of two forms:

* ``Closes #N`` - the PR delivers the issue. GitHub closes the issue when the
  PR merges.
* ``Refs #N`` - the PR delivers part of the issue. The coding agent asked for
  this with ``coding-done completed --partial`` (#7288). The issue stays open
  after the merge, and the next PR continues it.

This module is the only place that writes or reads that line. PR-to-issue
linking uses it, and so does the awaiting-merge rule that decides whether a
merged PR finished its issue. The body is the durable record: a restart, or a
maintainer who changes ``Closes`` to ``Refs`` by hand, gives the same answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_CLOSES_KEYWORD = "Closes"
_REFS_KEYWORD = "Refs"

# The two forms the orchestrator writes. Each links a PR to its issue.
_CLOSES_RE = re.compile(r"\bCloses\s+#(\d+)\b", re.IGNORECASE)
_REFS_RE = re.compile(r"\bRefs\s+#(\d+)\b", re.IGNORECASE)

# Every keyword GitHub accepts as a closing reference, with or without the colon
# it also accepts, naming the issue as ``#N`` or ``owner/repo#N``. A body that
# uses any of them for the issue is closed by GitHub on merge, so it is never
# partial, whatever else the body says. (An ``owner/repo#N`` for another
# repository errs toward "not partial", which keeps the close check.)
_GITHUB_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)\b",
    re.IGNORECASE,
)


def issue_reference_line(issue_number: int, *, partial: bool) -> str:
    """The first line of an orchestrator PR body for ``issue_number``."""
    keyword = _REFS_KEYWORD if partial else _CLOSES_KEYWORD
    return f"{keyword} #{issue_number}"


def linked_issue_number(body: str) -> int | None:
    """The issue a PR body links to, or None when it names none.

    A closing reference wins over a partial one, so a body that says both
    links to the issue it closes.
    """
    match = _CLOSES_RE.search(body) or _REFS_RE.search(body)
    return int(match.group(1)) if match else None


def body_links_issue(body: str, issue_numbers: Iterable[int]) -> bool:
    """Whether the body links (closing or partial) any of ``issue_numbers``."""
    wanted = set(issue_numbers)
    return any(
        int(match.group(1)) in wanted
        for pattern in (_CLOSES_RE, _REFS_RE)
        for match in pattern.finditer(body)
    )


def declares_partial_delivery(body: str, issue_number: int) -> bool:
    """Whether a PR body says it delivers only part of ``issue_number``.

    True when the body refers to the issue with ``Refs #N`` and names it in
    no GitHub closing reference. A merged PR like this does not finish its
    issue, so the orchestrator must neither close the issue nor treat it as
    done. A body that names the issue in no form at all is not partial: that
    is a broken closing reference, and the close-on-merge fallback still
    handles it.
    """
    closes = {int(m.group(1)) for m in _GITHUB_CLOSING_RE.finditer(body)}
    if issue_number in closes:
        return False
    return any(int(m.group(1)) == issue_number for m in _REFS_RE.finditer(body))
