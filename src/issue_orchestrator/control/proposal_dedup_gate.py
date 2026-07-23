"""The deterministic owner of the tech-lead ``create_issue`` dedup decision (#6878).

A pure gate: no GitHub, no database, no clock, no agent I/O. Observation/cache
code supplies TRUSTED facts (the open-issue corpus, the duplicate-target grant);
the agent supplies UNTRUSTED intent (``duplicate_of``); this gate owns the whole
decision and returns a typed outcome. The planner only translates that outcome
into existing actions — no dedup policy lives in the planner.

Why a typed outcome instead of ``(issue_number, bool)`` + a reasonless
``gate_issue`` flag (the review's B3/A1): it makes the dangerous states
UNREPRESENTABLE. In particular:

* ``CommentExisting`` — an external write to an existing issue — is only ever
  produced when the candidate is a KNOWN OPEN ISSUE (corpus membership proves it
  exists, is open, and is an issue not a PR), is inside an explicit
  duplicate-target GRANT derived from launch authority (not from treating
  ``create_issue`` as scope-free), AND both the ``create_issue`` effect and the
  ``post_comment`` surface are ``execute`` (a create→comment transform can never
  bypass ``post_comment`` authority).
* An UNAVAILABLE corpus never degrades to "no duplicate found": an agent citation
  is surfaced (gated) for later verification, never auto-committed and never
  silently filed as novel.
* A lexical gate always carries its candidate, score, and reason.
* Identical inputs always produce identical outcomes.

If a future model-backed semantic judge is added, that external judge warrants a
port; its judgment still enters this gate as UNTRUSTED input alongside
``duplicate_of``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..domain.tech_lead_session import TechLeadSessionFlavor
from .proposal_dedup import OpenIssueRef, find_duplicate

# --- inputs -----------------------------------------------------------------


class CorpusState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OpenIssueCorpus:
    """The trusted open-issue fact set the gate reasons against.

    ``READY`` carries the vetted open issues. ``UNAVAILABLE`` means the fact set
    could not be produced (e.g. increment 1, before the SQL fingerprint cache) —
    a DISTINCT state from "ready and empty", so the gate never confuses "cannot
    verify" with "verified no duplicate".
    """

    state: CorpusState
    issues: tuple[OpenIssueRef, ...] = ()

    @classmethod
    def unavailable(cls) -> "OpenIssueCorpus":
        return cls(CorpusState.UNAVAILABLE, ())

    @classmethod
    def ready(cls, issues: Sequence[OpenIssueRef]) -> "OpenIssueCorpus":
        return cls(CorpusState.READY, tuple(issues))

    @property
    def numbers(self) -> frozenset[int]:
        return frozenset(ref.number for ref in self.issues)


@dataclass(frozen=True)
class DuplicateTargetGrant:
    """Which trusted open issues a dedup redirect may target.

    Derived from launch authority, NEVER from treating ``create_issue`` as
    scope-free. ``whole_board`` (a health review that walks the entire board)
    grants the whole trusted corpus; otherwise no dedup target is granted — a
    batch or failure-investigation session may not redirect a proposal onto an
    arbitrary board issue.
    """

    board_wide: bool

    @classmethod
    def none(cls) -> "DuplicateTargetGrant":
        return cls(board_wide=False)

    @classmethod
    def whole_board(cls) -> "DuplicateTargetGrant":
        return cls(board_wide=True)

    @classmethod
    def for_flavor(cls, flavor: TechLeadSessionFlavor) -> "DuplicateTargetGrant":
        """Derive the grant from launch authority (#6878 B1): only a HEALTH_REVIEW
        — which walks the whole board — may redirect a proposal onto an arbitrary
        board issue. A batch review or failure investigation gets no grant."""
        return (
            cls.whole_board()
            if flavor is TechLeadSessionFlavor.HEALTH_REVIEW
            else cls.none()
        )

    def permits(self, issue_number: int, corpus: OpenIssueCorpus) -> bool:
        return (
            corpus.state is CorpusState.READY
            and issue_number in corpus.numbers
            and self.board_wide
        )


@dataclass(frozen=True)
class DedupAuthority:
    """The two authority modes that bound the concrete dedup effect."""

    create_issue_execute: bool
    post_comment_execute: bool

    @property
    def may_comment_now(self) -> bool:
        # An immediate external comment materializes the create_issue effect AS a
        # post_comment, so it requires BOTH to be execute — never under any
        # propose posture (the review's B2).
        return self.create_issue_execute and self.post_comment_execute


@dataclass(frozen=True)
class ProposalIntent:
    """The validated-but-untrusted ``create_issue`` intent."""

    title: str
    body: str
    duplicate_of: int | None


# --- typed outcome ----------------------------------------------------------


@dataclass(frozen=True)
class FileNew:
    """Novel proposal — file it normally (subject to create_issue authority)."""


@dataclass(frozen=True)
class CommentExisting:
    """Route the observation onto a verified, granted duplicate as a comment."""

    issue_number: int
    reason: str


@dataclass(frozen=True)
class GateSuspectedDuplicate:
    """Create a gated issue that names the candidate, score, and reason for a
    human to reconcile. ``score`` is ``None`` for an agent citation that could
    not be scored (e.g. corpus unavailable)."""

    issue_number: int
    score: float | None
    reason: str


@dataclass(frozen=True)
class RejectCandidate:
    """A provably-bad agent citation (missing / closed / PR / out-of-grant).
    Never comment on it — the planner routes this to a safe gated create."""

    issue_number: int
    reason: str


DedupOutcome = FileNew | CommentExisting | GateSuspectedDuplicate | RejectCandidate


# --- the gate ---------------------------------------------------------------


def classify_proposal(
    intent: ProposalIntent,
    corpus: OpenIssueCorpus,
    grant: DuplicateTargetGrant,
    authority: DedupAuthority,
    *,
    threshold: float,
) -> DedupOutcome:
    """Own the complete dedup decision for one ``create_issue`` proposal."""
    duplicate_of = intent.duplicate_of

    if corpus.state is CorpusState.UNAVAILABLE:
        # Cannot verify anything. A citation is surfaced (gated) for later
        # verification — never auto-committed, never silently filed as novel.
        if duplicate_of is not None:
            return GateSuspectedDuplicate(
                duplicate_of,
                None,
                "agent-cited duplicate; open-issue corpus unavailable — gated"
                " for verification",
            )
        return FileNew()

    if duplicate_of is not None:
        if duplicate_of not in corpus.numbers:
            # Missing, closed, or a PR number: not a known OPEN ISSUE.
            return RejectCandidate(
                duplicate_of, "cited duplicate is not a known open issue"
            )
        if not grant.permits(duplicate_of, corpus):
            return RejectCandidate(
                duplicate_of, "cited duplicate is outside the duplicate-target grant"
            )
        if authority.may_comment_now:
            return CommentExisting(duplicate_of, "agent-confirmed duplicate")
        return GateSuspectedDuplicate(
            duplicate_of,
            None,
            "agent-confirmed duplicate; propose authority — gated for approval",
        )

    match = find_duplicate(intent.title, intent.body, corpus.issues, threshold=threshold)
    if match is None or not grant.permits(match.number, corpus):
        return FileNew()
    return GateSuspectedDuplicate(
        match.number,
        match.score,
        f"lexical near-duplicate (score {match.score:.2f})",
    )
