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
_LINK_RE = re.compile(r"\b(?:Closes|Refs)\s+#(\d+)\b", re.IGNORECASE)
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

    The first link in body order wins. The orchestrator's own reference is
    the body's first line, so text further down (the agent's implementation
    notes, or a second "Closes #M") cannot take the PR away from its issue.
    """
    match = _LINK_RE.search(body)
    return int(match.group(1)) if match else None


def body_links_issue(body: str, issue_numbers: Iterable[int]) -> bool:
    """Whether the body links (closing or partial) any of ``issue_numbers``."""
    wanted = set(issue_numbers)
    return any(int(match.group(1)) in wanted for match in _LINK_RE.finditer(body))


def names_issue_in_closing_keyword(text: str, issue_number: int) -> bool:
    """Whether ``text`` names the issue in a keyword GitHub closes it by.

    GitHub applies these in a PR body when the PR merges, and in a commit
    message when the commit reaches the default branch. A partial delivery
    must not contain one anywhere.
    """
    return any(int(m.group(1)) == issue_number for m in _GITHUB_CLOSING_RE.finditer(text))


def declares_partial_delivery(body: str, issue_number: int) -> bool:
    """Whether a PR body says it delivers only part of ``issue_number``.

    True when the body refers to the issue with ``Refs #N`` and names it in
    no GitHub closing reference. A merged PR like this does not finish its
    issue, so the orchestrator must neither close the issue nor treat it as
    done. A body that names the issue in no form at all is not partial: that
    is a broken closing reference, and the close-on-merge fallback still
    handles it.
    """
    if names_issue_in_closing_keyword(body, issue_number):
        return False
    return any(int(m.group(1)) == issue_number for m in _REFS_RE.finditer(body))


def honors_partial_claim(body: str, issue_number: int, *, partial: bool) -> bool:
    """Whether an existing PR's body can carry a completion's partial claim.

    Publication can reuse a PR that an earlier session opened. Only a partial
    claim can be broken by that PR: if its body still closes the issue,
    merging it would close an issue the agent says is not finished. A
    completion that makes no partial claim keeps whatever the PR already
    says, because leaving an issue open is recoverable and closing it is not.
    """
    return not partial or declares_partial_delivery(body, issue_number)
