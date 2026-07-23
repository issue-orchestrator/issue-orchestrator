"""The deterministic owner of the tech-lead ``create_issue`` dedup decision (#6878).

A pure gate: no GitHub, no database, no clock, no agent I/O. Observation/cache
code supplies TRUSTED facts (the open-issue corpus, the immutable redirect grant);
the agent supplies UNTRUSTED intent (``duplicate_of``); this gate owns the whole
decision and returns a typed outcome. The planner only translates that outcome
into existing actions — no dedup policy lives in the planner.

A typed outcome (instead of ``(issue_number, bool)`` + a reasonless ``gate_issue``
flag) makes the dangerous states UNREPRESENTABLE:

* ``CommentExisting`` — an external write to an existing issue — is only ever
  produced when the candidate is a KNOWN OPEN ISSUE (corpus membership proves it
  exists, is open, and is an issue not a PR), is inside the session's IMMUTABLE
  redirect grant (its existing comment scope from launch authority — never a new
  capability synthesized at completion), AND both the ``create_issue`` effect and
  the ``post_comment`` surface are ``execute``.
* Duplicate EVIDENCE (a lexical match, or a verified-but-uncommentable citation)
  gates a NEW issue — it writes nothing to the candidate, so it needs no redirect
  grant. Gating is available to every session flavor.
* The corpus is a three-state fact: ``READY`` (dedup evaluated), ``DISABLED`` (the
  trusted corpus / lexical backstop is intentionally off — ``FileNew`` when there
  is no citation, but a cited ``duplicate_of`` still GATES with its candidate),
  and ``UNAVAILABLE`` (facts were expected but could not be produced).
  ``UNAVAILABLE`` FAILS CLOSED — it can never degrade to "no duplicate found" and
  file an unchecked proposal. A citation is never discarded by a non-``READY``
  corpus.
* Every gate carries its reason; a lexical gate also carries candidate + score.
* Identical inputs always produce identical outcomes.

A future model-backed semantic judge would warrant a port; its judgment still
enters this gate as UNTRUSTED input alongside ``duplicate_of``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .proposal_dedup import OpenIssueRef, find_duplicate

# --- inputs -----------------------------------------------------------------


class CorpusState(StrEnum):
    READY = "ready"          # dedup was actually evaluated against trusted facts
    DISABLED = "disabled"    # the feature is intentionally off (e.g. increment 1)
    UNAVAILABLE = "unavailable"  # facts were expected but could not be produced


def _is_corpus_state(value: object) -> bool:
    # ``object`` param so the runtime check is real at the construction boundary
    # (a ``cast(CorpusState, "unavailable")`` is a plain str, not a member) and
    # not narrowed away by the declared field type.
    return isinstance(value, CorpusState)


@dataclass(frozen=True)
class OpenIssueCorpus:
    """The trusted open-issue fact set the gate reasons against.

    ``READY`` carries the vetted open issues. ``DISABLED`` and ``UNAVAILABLE``
    carry none — and the two are NOT interchangeable: ``DISABLED`` is the intended
    off state, while ``UNAVAILABLE`` is a fact-production failure that must fail
    closed. The state/data invariant is enforced so a contradictory corpus (an
    unavailable corpus that nonetheless lists issues) cannot be constructed.
    """

    state: CorpusState
    issues: tuple[OpenIssueRef, ...] = ()

    def __post_init__(self) -> None:
        if not _is_corpus_state(self.state):
            raise TypeError(f"corpus state must be a CorpusState, got {self.state!r}")
        if self.state is not CorpusState.READY and self.issues:
            raise ValueError(f"a {self.state} corpus must carry no issues")

    @classmethod
    def disabled(cls) -> "OpenIssueCorpus":
        return cls(CorpusState.DISABLED, ())

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
    """The immutable set of issues a dedup REDIRECT (``CommentExisting``) may
    target — the session's EXISTING comment scope from launch authority
    (``allowed_targets()``), never a new capability. A verified duplicate outside
    it is still gated (writes nothing), so it is not in this set.
    """

    targets: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def none(cls) -> "DuplicateTargetGrant":
        return cls(frozenset())

    @classmethod
    def of(cls, targets: Iterable[int]) -> "DuplicateTargetGrant":
        return cls(frozenset(targets))

    def permits(self, issue_number: int) -> bool:
        return issue_number in self.targets


@dataclass(frozen=True)
class DedupAuthority:
    """The two authority modes that bound the concrete dedup effect."""

    create_issue_execute: bool
    post_comment_execute: bool

    @property
    def may_comment_now(self) -> bool:
        # An immediate external comment materializes the create_issue effect AS a
        # post_comment, so it requires BOTH to be execute — never under any
        # propose posture.
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
    """Create a gated issue that names the candidate, reason, and (for a lexical
    match) score for a human to reconcile. ``score`` is ``None`` for a verified
    agent citation that is gated because it cannot be commented."""

    issue_number: int
    score: float | None
    reason: str


@dataclass(frozen=True)
class GateDedupUnavailable:
    """Dedup could not be evaluated (facts expected but unavailable) and there is
    no candidate. Fail closed: gate the new issue for human review rather than
    file it unchecked."""

    reason: str


@dataclass(frozen=True)
class GateUnverifiedDuplicate:
    """The agent cited a duplicate but no trusted corpus could verify it (the
    lexical backstop is dormant, or the corpus is unavailable). Its evidence is
    NOT discarded into a novel issue and NOT auto-routed as a comment: gate the
    create, retaining the candidate the operator needs to reconcile."""

    issue_number: int
    reason: str


@dataclass(frozen=True)
class RejectCandidate:
    """A provably-bad agent citation (not a known open issue). Never comment on
    it — the planner routes this to a safe gated create."""

    issue_number: int
    reason: str


DedupOutcome = (
    FileNew
    | CommentExisting
    | GateSuspectedDuplicate
    | GateDedupUnavailable
    | GateUnverifiedDuplicate
    | RejectCandidate
)


# --- the gate ---------------------------------------------------------------


def _uncommentable_reason(commentable: bool, authority: DedupAuthority) -> str:
    """Name the specific mode(s) that block an immediate comment, so the gated
    issue's evidence is precise (not a blanket "propose authority")."""
    causes: list[str] = []
    if not commentable:
        causes.append("target outside this session's comment scope")
    if not authority.create_issue_execute:
        causes.append("create_issue=propose")
    if not authority.post_comment_execute:
        causes.append("post_comment=propose")
    return "; ".join(causes)


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

    if corpus.state is CorpusState.DISABLED:
        # The lexical backstop is intentionally dormant (increment 1). An agent
        # citation cannot be verified without a trusted corpus, so it is NOT
        # auto-routed — but its evidence is kept and gated, never discarded into a
        # novel issue.
        if duplicate_of is not None:
            return GateUnverifiedDuplicate(
                duplicate_of,
                "agent-cited duplicate; not yet verifiable (dedup corpus not"
                " enabled)",
            )
        return FileNew()

    if corpus.state is CorpusState.UNAVAILABLE:
        # Facts expected but missing: fail closed, never file unchecked. A citation
        # is retained so the operator keeps the exact candidate to reconcile.
        if duplicate_of is not None:
            return GateUnverifiedDuplicate(
                duplicate_of,
                "agent-cited duplicate; open-issue corpus unavailable — could not"
                " verify",
            )
        return GateDedupUnavailable(
            "open-issue corpus unavailable — duplicates could not be verified"
        )

    if duplicate_of is not None:
        if duplicate_of not in corpus.numbers:
            # Missing, closed, or a PR number: not a known OPEN ISSUE.
            return RejectCandidate(
                duplicate_of, "cited duplicate is not a known open issue"
            )
        # A verified open-issue duplicate. Commenting requires the redirect grant
        # AND execute/execute; otherwise gate (writes nothing to the candidate).
        commentable = grant.permits(duplicate_of)
        if commentable and authority.may_comment_now:
            return CommentExisting(duplicate_of, "agent-confirmed duplicate")
        return GateSuspectedDuplicate(
            duplicate_of,
            None,
            "agent-confirmed duplicate; gated ("
            + _uncommentable_reason(commentable, authority)
            + ")",
        )

    # No citation: the lexical backstop gates any strong match, for every flavor —
    # the grant governs writes (CommentExisting), not whether evidence may gate.
    match = find_duplicate(intent.title, intent.body, corpus.issues, threshold=threshold)
    if match is None:
        return FileNew()
    return GateSuspectedDuplicate(match.number, match.score, "lexical near-duplicate")
