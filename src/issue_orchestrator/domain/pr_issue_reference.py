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
from dataclasses import dataclass

_CLOSES_KEYWORD = "Closes"
_REFS_KEYWORD = "Refs"

# ONE grammar for every issue link in PR text (#7288 round 5). A link is a
# keyword, an optional colon, then the issue as ``#N``, ``owner/repo#N`` or
# ``https://github.com/owner/repo/issues/N``. The keyword is either one GitHub
# closes the issue by (close/closes/closed, fix/fixes/fixed, resolve/resolves/
# resolved) or ``Refs`` for a partial delivery. A bare ``#N`` mention is never a
# link. Every question below (which issue owns the PR, which issues a PR is
# working on, whether it closes or only refs an issue) reads these same
# tokens, filtered to THIS repository, so no two paths can disagree about what
# a local issue reference is.
_LINK_RE = re.compile(
    r"\b(?P<keyword>close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs):?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)#|https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/|#)"
    r"(?P<number>\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IssueLink:
    """One issue link found in PR text."""

    number: int
    closes: bool  # a GitHub closing keyword; False for ``Refs``
    repo: str | None  # the qualifying ``owner/repo``, None when unqualified


def issue_links(text: str) -> tuple[IssueLink, ...]:
    """Every issue link in ``text``, in text order, for any repository."""
    links: list[IssueLink] = []
    for match in _LINK_RE.finditer(text):
        repo = match.group("repo") or match.group("url_repo")
        links.append(IssueLink(
            number=int(match.group("number")),
            closes=match.group("keyword").lower() != "refs",
            repo=repo,
        ))
    return tuple(links)


def _require_slug(repo_slug: str) -> str:
    if "/" not in repo_slug.strip():
        raise ValueError(f"issue links need an owner/repo slug, got {repo_slug!r}")
    return repo_slug.strip().casefold()


def local_issue_links(text: str, *, repo_slug: str) -> tuple[IssueLink, ...]:
    """The links in ``text`` that name an issue of ``repo_slug``: unqualified,
    or qualified with this repository. ``Closes other/repo#200`` is not a
    link to local #200."""
    slug = _require_slug(repo_slug)
    return tuple(
        link for link in issue_links(text)
        if link.repo is None or link.repo.casefold() == slug
    )


def issue_reference_line(issue_number: int, *, partial: bool) -> str:
    """The first line of an orchestrator PR body for ``issue_number``."""
    keyword = _REFS_KEYWORD if partial else _CLOSES_KEYWORD
    return f"{keyword} #{issue_number}"


def linked_issue_number(body: str, *, repo_slug: str) -> int | None:
    """The local issue that OWNS a PR, from its body, or None.

    The first local link in body order wins. The orchestrator's own reference
    is the body's first line, so text further down cannot take the PR away
    from its issue.
    """
    links = local_issue_links(body, repo_slug=repo_slug)
    return links[0].number if links else None


def linked_issue_numbers(body: str, *, repo_slug: str) -> frozenset[int]:
    """EVERY local issue a PR body links, closing or partial.

    Not :func:`linked_issue_number`, which answers the single issue that owns
    the PR. This answers which issues an open PR is still working on, so a
    gate kept over them (Retry's pr-pending) covers all of them.
    """
    return frozenset(link.number for link in local_issue_links(body, repo_slug=repo_slug))


def body_links_issue(body: str, issue_numbers: Iterable[int], *, repo_slug: str) -> bool:
    """Whether the body links (closing or partial) any of ``issue_numbers``."""
    return not linked_issue_numbers(body, repo_slug=repo_slug).isdisjoint(set(issue_numbers))


def names_issue_in_closing_keyword(text: str, issue_number: int, *, repo_slug: str) -> bool:
    """Whether ``text`` closes local ``issue_number`` by a GitHub keyword.

    GitHub applies these in a PR body when the PR merges, and in a commit
    message when the commit reaches the default branch. A partial delivery
    must not contain one anywhere.
    """
    return any(
        link.closes and link.number == issue_number
        for link in local_issue_links(text, repo_slug=repo_slug)
    )


def declares_partial_delivery(body: str, issue_number: int, *, repo_slug: str) -> bool:
    """Whether a PR body says it delivers only part of local ``issue_number``.

    True when the body refs the issue and closes it by no keyword. A merged
    PR like this does not finish its issue, so the orchestrator must neither
    close the issue nor treat it as done. A body that names the issue in no
    form at all is not partial: that is a broken closing reference, and the
    close-on-merge fallback still handles it.
    """
    links = [
        link for link in local_issue_links(body, repo_slug=repo_slug)
        if link.number == issue_number
    ]
    return bool(links) and not any(link.closes for link in links)


def honors_partial_claim(
    body: str, issue_number: int, *, partial: bool, repo_slug: str
) -> bool:
    """Whether an existing PR's body can carry a completion's partial claim.

    Publication can reuse a PR that an earlier session opened. Only a partial
    claim can be broken by that PR: if its body still closes the issue,
    merging it would close an issue the agent says is not finished. A
    completion that makes no partial claim keeps whatever the PR already
    says, because leaving an issue open is recoverable and closing it is not.
    """
    return not partial or declares_partial_delivery(body, issue_number, repo_slug=repo_slug)
