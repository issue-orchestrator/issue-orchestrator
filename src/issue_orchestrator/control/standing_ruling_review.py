"""The review rule of standing rulings (#8141): an approval must attest them.

A maintainer ruling binds the PR. porchpin#379's reviewer approved a diff that
extended exactly the design the ruling retired, twice, because nothing asked
the review about the ruling. So an approval of a diff that touches what a
standing ruling governs (its files, or anything when it governs the whole
issue) must attest that ruling as upheld; one that does not is REFUSED: the
review becomes a changes-requested review whose implementation-required
feedback is the ruling, so the PR goes back to rework with it as the brief.

Both review paths ask this one rule, before any of the review's actions run:

* a post-publish review's ``reviewer-done approved`` record, at the
  completion door (:meth:`StandingRulingsReview.admit`);
* a review exchange's ``ok`` verdict, at its approval gate
  (:meth:`StandingRulingsReview.exchange_gate`), which turns a refused
  approval into another coder round.

An approval the exchange cached is judged again before it is reused: the
rulings standing then, against the attestations its own decision made.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import CompletionOutcome, RequestedAction
from ..domain.standing_ruling import refused_approval_feedback, rulings_covering
from ..infra.logging_config import issue_log

if TYPE_CHECKING:
    from ..domain.models import CompletionRecord
    from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
    from ..domain.standing_ruling import StandingRuling
    from ..ports.standing_rulings import StandingRulings

logger = logging.getLogger(__name__)

#: What a changes-requested review asks of the orchestrator (reviewer-done's own set).
_CHANGES_REQUESTED_ACTIONS = (
    RequestedAction.ADD_NEEDS_REWORK_LABEL,
    RequestedAction.REMOVE_CODE_REVIEW_LABEL,
    RequestedAction.POST_COMMENT,
)

#: Every path the reviewed branch's diff touches, against the base its PR (the
#: given number, else the branch's own) merges into NOW; raises when git or
#: GitHub cannot say.
ChangedPaths = Callable[[Path, "int | None"], tuple[str, ...]]


def _comment(feedback: str, risk: str | None) -> str:
    return (
        "## Changes Requested\n\n"
        "The reviewer approved this change, but the orchestrator refused the approval:\n\n"
        f"{feedback}\n\n---\n<!-- VERDICT_START -->\n**Verdict:** `request_changes`\n"
        f"**Risk:** `{risk or 'unknown'}`\n<!-- VERDICT_END -->\n\n"
        "*The work agent will be re-queued to address these issues.*"
    )


def refuse_approval(record: "CompletionRecord", refused: tuple["StandingRuling", ...]) -> "CompletionRecord":
    """*record* (an approval) turned into the changes-requested review it must be."""
    feedback = refused_approval_feedback(refused, summary=record.review_summary)
    return replace(
        record,
        outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
        summary=f"Approval refused by standing rulings: {', '.join(r.ruling_id for r in refused)}",
        requested_actions=list(_CHANGES_REQUESTED_ACTIONS),
        review_summary=None,
        review_issues=feedback,
        checks_passed=None,
        upheld_rulings=None,
        comment_body=_comment(feedback, record.risk_level),
    )


@dataclass(frozen=True)
class StandingRulingsReview:
    """The review rule, for both review paths (module docstring)."""

    rulings: "StandingRulings"
    changed_paths: ChangedPaths

    def refused(
        self, issue_number: int, worktree: Path, attested: tuple[str, ...], *, pr_number: int | None = None,
    ) -> tuple["StandingRuling", ...]:
        """The rulings covering the reviewed diff that an approval attesting *attested* leaves unattested."""
        rulings = self.rulings.active(issue_number)
        if not rulings:
            return ()  # nothing binds the issue: no diff to read
        paths = self.changed_paths(worktree, pr_number)
        # No branch diff (a retrospective review of merged work reviews the code
        # as it stands): every ruling governs what was reviewed.
        covering = rulings_covering(rulings, paths) if paths else rulings
        return tuple(ruling for ruling in covering if ruling.ruling_id not in attested)

    def admit(
        self, record: "CompletionRecord", *, issue_number: int, worktree: Path, pr_number: int | None,
    ) -> "CompletionRecord":
        """A post-publish review's record to act on: the approval, or its refusal."""
        if not record.approves:
            return record
        refused = self.refused(issue_number, worktree, tuple(record.upheld_rulings or ()), pr_number=pr_number)
        if not refused:
            return record
        logger.warning(issue_log(issue_number, "Approval refused: standing rulings %s not attested"),
                       [ruling.ruling_id for ruling in refused])
        return refuse_approval(record, refused)

    def exchange_gate(
        self, issue_number: int, worktree: Path, other: "ReviewExchangeApprovalGate | None"
    ) -> "ReviewExchangeApprovalGate":
        """A review exchange's approval gate: *other*'s policy, then the review rule."""
        return _ExchangeApprovalGate(self, issue_number, worktree, other)


@dataclass(frozen=True)
class _ExchangeApprovalGate:
    """*other*'s policy first (the Tech Lead's artifact gate), then the review rule."""

    review: StandingRulingsReview
    issue_number: int
    worktree: Path
    other: "ReviewExchangeApprovalGate | None"

    def rejection_reason(self, *, upheld_rulings: tuple[str, ...]) -> str | None:
        first = None if self.other is None else self.other.rejection_reason(upheld_rulings=upheld_rulings)
        return first or self._refusal(upheld_rulings)

    def cached_approval_reason(self, *, upheld_rulings: tuple[str, ...]) -> str | None:
        """A cached approval is judged again on the rulings standing NOW, with the
        attestations its own decision made (a ruling recorded since refuses it)."""
        first = None if self.other is None else self.other.cached_approval_reason(upheld_rulings=upheld_rulings)
        return first or self._refusal(upheld_rulings)

    def _refusal(self, upheld_rulings: tuple[str, ...]) -> str | None:
        refused = self.review.refused(self.issue_number, self.worktree, upheld_rulings)
        return refused_approval_feedback(refused, summary=None) if refused else None
