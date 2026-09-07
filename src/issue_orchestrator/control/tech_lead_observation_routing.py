"""Where a duplicate-suspected ``create_issue`` observation lands (#6989).

The dedup gate (#6878) owns *whether* a ``create_issue`` proposal is a
duplicate. It has exactly one non-filing route for a duplicate it recognizes:
:class:`~.proposal_dedup_gate.CommentExisting`, which requires the candidate to
be inside the session's launch comment grant. For a HEALTH REVIEW that grant is
``{anchor}`` — so a re-sighting of a STANDING problem (a tracker the session
does not own) can never take that route, and every remaining outcome fell
through to "create a gated issue naming the candidate".

That is why the board grew one new open issue per day per standing problem
(#6989): the open-issue list became the append-only log of known problems,
burying genuinely new work and making every later dedup check harder.

This module is the single owner of the missing route. A duplicate the AGENT
itself cited — the only case where dedup intent exists rather than being
inferred — accrues onto the durable **pattern case-file ledger** (#6781)
instead of minting a fresh issue:

* the first sighting of a standing problem opens ONE case file for it,
* every later sighting lands there as an evidence comment with a durable,
  create-once observation count,
* the case file names the candidate/tracking issue, so it is the human's
  reconciliation queue rather than N unreconciled open issues.

Bounded on purpose:

* Only the two AGENT-CITED outcomes route here —
  :class:`~.proposal_dedup_gate.GateSuspectedDuplicate` with no score (a
  corpus-VERIFIED citation the session may not comment on) and
  :class:`~.proposal_dedup_gate.GateUnverifiedDuplicate` (a citation no trusted
  corpus could verify). A LEXICAL suspicion (a scored
  ``GateSuspectedDuplicate``) carries no agent intent, so it keeps the gated
  create: burying a false-positive match in an evidence ledger would lose real
  work, which is the same failure this issue is about, only inverted.
* :class:`~.proposal_dedup_gate.RejectCandidate` (a provably-bad citation) and
  :class:`~.proposal_dedup_gate.GateDedupUnavailable` (no candidate at all) name
  no accrual point and keep failing closed into a gated create.
* Accrual is a ``flag_pattern`` effect — it writes only orchestrator-owned
  observation ledgers — so it requires ``flag_pattern`` execute authority and
  grants no capability that authority does not already grant. The caller
  enforces that; see ``tech_lead_decision_actions``.
* A sighting CLASSIFIES NOTHING AND DIAGNOSES NOTHING. It enters the case-file
  lane as an evidence-only
  :class:`~.tech_lead_case_files.CaseFileIntake`, so no immutable ledger field
  (``fix_class``, ``area``) and no canonical ``diagnosis`` can come from it —
  see :func:`case_file_sighting` for why letting one through would abort whole
  decisions, steer promotion routing, and let a promotion be filed on evidence
  that diagnosed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import assert_never

from ..domain.tech_lead_artifacts import ProposedTechLeadAction
from .proposal_dedup_gate import (
    CommentExisting,
    DedupOutcome,
    FileNew,
    GateDedupUnavailable,
    GateSuspectedDuplicate,
    GateUnverifiedDuplicate,
    RejectCandidate,
)
from .tech_lead_case_files import CaseFileIntake

# Prefix of the DERIVED accrual signature, used when the tech lead did not name
# the recurring class itself. Keyed by the cited issue so every sighting of the
# same standing problem reaches the same case file, restart after restart.
DEDUP_ACCRUAL_SIGNATURE_PREFIX = "duplicate-of-#"


@dataclass(frozen=True)
class LedgerAccrual:
    """The durable accrual point for one duplicate-suspected observation.

    ``signature`` keys the case-file ledger (one case file per recurring class);
    ``candidate_issue_number`` is the issue a human reconciles this cluster
    against; ``reason`` is the gate's own words for why the observation could
    not simply be routed onto that candidate as a comment.
    """

    signature: str
    candidate_issue_number: int
    reason: str


def accrual_for(
    outcome: DedupOutcome, proposed: ProposedTechLeadAction
) -> LedgerAccrual | None:
    """The ledger accrual this outcome earns, or ``None`` to keep the create.

    Matches on the TYPED outcome rather than on ``duplicate_of`` directly: the
    gate has already decided which citations are meaningful, and a scored
    :class:`GateSuspectedDuplicate` is a lexical suspicion the agent never
    claimed. Deriving the answer from the outcome keeps the "agent-cited only"
    rule in one place instead of re-deriving it from raw intent.

    Matched exhaustively against the ``DedupOutcome`` union: a future variant
    added without a routing decision here is a static exhaustiveness error and a
    runtime ``AssertionError``, never a silent fallthrough to "mint an issue" —
    which is the failure mode this whole module exists to end.
    """
    match outcome:
        case GateSuspectedDuplicate(issue_number=number, score=None, reason=reason):
            # An agent citation the trusted corpus VERIFIED, which this session
            # may not comment on (outside its grant, or a propose posture).
            return _accrual(proposed, number, reason)
        case GateUnverifiedDuplicate(issue_number=number, reason=reason):
            # An agent citation no trusted corpus could verify. The ledger keeps
            # the candidate for a human instead of guessing either way.
            return _accrual(proposed, number, reason)
        case GateSuspectedDuplicate():
            # Scored -> a LEXICAL suspicion with no agent intent behind it.
            return None
        case FileNew() | CommentExisting() | GateDedupUnavailable() | RejectCandidate():
            return None
        case _:
            assert_never(outcome)


def _accrual(
    proposed: ProposedTechLeadAction, candidate: int, reason: str
) -> LedgerAccrual:
    return LedgerAccrual(
        signature=accrual_signature(proposed, candidate),
        candidate_issue_number=candidate,
        reason=reason,
    )


def accrual_signature(proposed: ProposedTechLeadAction, candidate: int) -> str:
    """The case-file signature this observation accrues under.

    A tech lead that NAMES the recurring class on the proposal
    (``pattern_signature``) wins: that is how a standing problem gets one case
    file spanning several trackers, and how a ``create_issue`` sighting joins
    the case file an earlier ``flag_pattern`` already opened for the same class
    (#6989 scope item 2). Otherwise the candidate itself is the class, which
    needs nothing from the agent and is stable across restarts.

    The named signature is used VERBATIM — never normalized — because
    ``flag_pattern`` keys the same ledger with the raw value; normalizing here
    would split one recurring class across two case files.
    """
    return proposed.pattern_signature or f"{DEDUP_ACCRUAL_SIGNATURE_PREFIX}{candidate}"


def case_file_sighting(
    proposed: ProposedTechLeadAction,
    accrual: LedgerAccrual,
    *,
    sibling_action_id: str | None = None,
) -> CaseFileIntake:
    """Restate a ``create_issue`` proposal as the case-file sighting it is.

    The case-file lane composes its issue body, evidence comment, and durable
    observation identity from a proposal, so the accrual reuses that type for
    the EVIDENCE half rather than teaching the lane a second rendering shape.
    The action id (the observation's identity) and the linked findings stay the
    proposal's own; the signature and the body change.

    The durable half is stated separately, and this is the point of returning a
    :class:`~.tech_lead_case_files.CaseFileIntake`:
    :meth:`~.tech_lead_case_files.CaseFileIntake.sighting` establishes NOTHING,
    so one contract carries all three consequences instead of each builder
    remembering them:

    * ``fix_class`` is already impossible here — it is valid only on
      ``flag_pattern`` actions — so an accrued observation can never make a
      signature promotable.
    * ``area`` IS carriable by ``create_issue`` (the prompt asks for it on
      root-cause proposals), and it is one of the ledger's immutable fields:
      ``reconcile_pattern_classification`` raises on any disagreement, and that
      raise unwinds into WHOLE-DECISION rejection. Letting an unreconciled
      duplicate sighting supply it would (1) turn a soft tag on one re-sighting
      into an abort of every other action in the decision — precisely the
      workload #6989 is about, several daily sightings of one problem described
      from different angles — and (2) let a sighting that diagnosed nothing pick
      the repository a ``fix:code`` promotion is filed into.
    * the ``diagnosis`` — the actionable mechanism and suggested fix a routed
      promotion is FILED ON — likewise stays with ``flag_pattern``. A sighting's
      text says a known problem was seen again; treating it as the canonical
      diagnosis would let a promotion's central claim come from evidence that
      classified nothing (#6989 round-1 review F1).

    Classification and diagnosis are reviewed decisions. The proposal's claimed
    ``area`` is still not discarded: it is recorded in the observation text
    (with ``labels``/``expedite``, which the ledger also has no home for) so the
    evidence survives for the human reconciling the cluster.
    """
    return CaseFileIntake.sighting(
        replace(
            proposed,
            pattern_signature=accrual.signature,
            body=_observation_body(proposed, accrual, sibling_action_id),
        )
    )


def _observation_body(
    proposed: ProposedTechLeadAction,
    accrual: LedgerAccrual,
    sibling_action_id: str | None,
) -> str:
    """The observation text: the proposal verbatim plus its routing evidence.

    The proposal's TITLE is kept (the case-file body renders only the body
    text), and the candidate plus the gate's reason ride along so the ledger is
    reconcilable without re-deriving why the observation landed here. The
    proposal fields the ledger has no home for — its claimed ``area``, its
    proposed ``labels``, its ``expedite`` intent — are recorded here rather than
    silently lost, since the human reconciling the cluster is who acts on them.
    """
    lines = [f"**{proposed.title.strip()}**", ""] if proposed.title else []
    lines.append(proposed.body or "")
    lines.extend(
        [
            "",
            "---",
            "",
            "**Routed to this case file instead of a new issue (#6989).** The"
            " tech lead proposed this as a new issue and cited"
            f" #{accrual.candidate_issue_number} as a duplicate:"
            f" {accrual.reason}. Recurring classes accrue here so one standing"
            " problem keeps one durable ledger instead of one open issue per"
            f" sighting. Reconcile against #{accrual.candidate_issue_number};"
            " graduate this case file into work if the cluster turns out to be"
            " a distinct problem.",
        ]
    )
    claims = _unapplied_claims(proposed)
    if claims:
        lines.append(
            "\nThe proposal also asked for "
            + ", ".join(claims)
            + ". A sighting classifies nothing and schedules nothing, so these"
            " are recorded as evidence only — apply them when graduating this"
            " case file into work."
        )
    if sibling_action_id is not None:
        lines.append(
            f"\nAlso an intra-decision duplicate of proposal"
            f" {sibling_action_id} in the same tech-lead decision."
        )
    return "\n".join(lines)


def _unapplied_claims(proposed: ProposedTechLeadAction) -> list[str]:
    """The proposal's issue-shaped requests that a case file cannot honor."""
    claims: list[str] = []
    if proposed.area:
        claims.append(f"area `{proposed.area}`")
    if proposed.labels:
        claims.append("labels " + ", ".join(f"`{label}`" for label in proposed.labels))
    if proposed.expedite:
        claims.append("expedited scheduling")
    return claims
