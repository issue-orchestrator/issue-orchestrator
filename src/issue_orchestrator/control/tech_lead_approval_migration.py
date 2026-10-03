"""Startup migration of the legacy ``proposed-tech-lead`` gate (#7763).

The approval model replaced "remove ``proposed-tech-lead`` to approve" with a
positive, maintainer-applied ``approved`` label. Two kinds of open proposal
predate it:

1. **Still gated** — open and carrying ``proposed-tech-lead``. They move to the
   new labels with no behaviour change: provenance and waiting state are
   added, then the legacy label comes off (in that order, so a crash between
   the writes leaves the item gated and the next startup finishes the move).
2. **Approved under the old model but not yet executed** — an open proposal
   with a stored op and none of the approval labels: someone removed the
   legacy gate. The removal is honoured only if a MAINTAINER made it (the
   latest ``unlabeled`` event's actor): the engine applies ``approved`` and
   records that exact label event as the maintainer's approval, so the next
   tick executes it once. Any other remover (a bot, a retry, an agent)
   re-gates the item and says so.

Closed proposals — executed or declined under either model — are left alone.
Plain follow-ups whose gate was removed under the old model are ordinary
issues already and are not touched. The pass is idempotent: once migrated, no
open issue carries the legacy label and every op-backed proposal carries the
new provenance label, so a re-run finds nothing to do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.tech_lead_approval import (
    APPROVED_LABEL,
    AWAITING_APPROVAL_LABEL,
    TECH_LEAD_PROPOSAL_LABEL,
    OperatorApprovalRecord,
)
from .tech_lead_proposals import TECH_LEAD_PROPOSAL_SCAN_LIMIT

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_approval import TechLeadApprovals

logger = logging.getLogger(__name__)

#: The retired gate label. Named here and nowhere else in the engine.
LEGACY_GATE_LABEL = "proposed-tech-lead"


@dataclass(frozen=True)
class ApprovalMigrationReport:
    regated: tuple[int, ...] = ()
    legacy_approved: tuple[int, ...] = ()
    legacy_unapproved: tuple[int, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.regated or self.legacy_approved or self.legacy_unapproved)


class ApprovalMigrationError(RuntimeError):
    """Some legacy proposals could not be migrated; startup must not proceed."""

    def __init__(self, report: ApprovalMigrationReport) -> None:
        super().__init__(
            "tech-lead proposal approval migration incomplete: " + "; ".join(report.errors)
        )
        self.report = report


def _carries(issue: "Issue", label: str) -> bool:
    folded = label.casefold()
    return any(str(name).casefold() == folded for name in issue.labels)


def migrate_legacy_proposals(
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals",
    ops: "TechLeadAuthorityStore",
) -> ApprovalMigrationReport:
    """Move every open legacy proposal onto the approval model (idempotent)."""
    regated: list[int] = []
    approved: list[int] = []
    unapproved: list[int] = []
    errors: list[str] = []
    legacy = repository.list_issues(
        labels=[LEGACY_GATE_LABEL],
        state="open",
        limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        exhaustive=True,
    )
    for issue in legacy:
        try:
            _regate(repository, issue)
            regated.append(issue.number)
        except Exception as exc:  # migrate the rest, then fail the startup
            logger.exception("[tech_lead] Migrating legacy proposal #%d failed", issue.number)
            errors.append(f"#{issue.number}: {exc}")
    for number, _op in ops.list_ops():
        if number in regated:
            continue
        try:
            outcome = _migrate_legacy_approval(repository, approvals, number)
        except Exception as exc:
            logger.exception("[tech_lead] Migrating op-backed proposal #%d failed", number)
            errors.append(f"#{number}: {exc}")
            continue
        if outcome is True:
            approved.append(number)
        elif outcome is False:
            unapproved.append(number)
    report = ApprovalMigrationReport(
        tuple(regated), tuple(approved), tuple(unapproved), tuple(errors)
    )
    if report.errors:
        # Fail fast: the legacy label no longer blocks anything, so a proposal
        # left unmigrated would be schedulable. Everything migratable was
        # migrated above; the next startup retries the rest.
        raise ApprovalMigrationError(report)
    if report.changed:
        logger.info(
            "[tech_lead] Approval label migration: regated=%s legacy_approved=%s"
            " legacy_unapproved=%s errors=%s",
            report.regated,
            report.legacy_approved,
            report.legacy_unapproved,
            report.errors,
        )
    return report


def _regate(repository: "RepositoryHost", issue: "Issue") -> None:
    for label in (TECH_LEAD_PROPOSAL_LABEL, AWAITING_APPROVAL_LABEL):
        if not _carries(issue, label):
            repository.add_label(issue.number, label)
    for label in issue.labels:
        if str(label).casefold() == LEGACY_GATE_LABEL.casefold():
            repository.remove_label(issue.number, label)


def _migrate_legacy_approval(
    repository: "RepositoryHost", approvals: "TechLeadApprovals", number: int
) -> bool | None:
    """None: nothing to migrate. True: honoured as approved. False: re-gated.

    Resumable without a journal (#7763 review F1/F4): the approval half is
    written BEFORE the provenance label, so a crash anywhere before the
    provenance write leaves the issue looking exactly as it did ("no approval
    labels"), and the next startup redoes the step, re-reading what is there.
    """
    issue = repository.get_issue(number)
    if issue is None or issue.state != "open" or _carries(issue, LEGACY_GATE_LABEL):
        return None
    if _carries(issue, TECH_LEAD_PROPOSAL_LABEL) or _carries(issue, AWAITING_APPROVAL_LABEL):
        return None
    removal = approvals.evidence.latest_label_event(number, LEGACY_GATE_LABEL, removed=True)
    if removal is not None and approvals.is_maintainer(removal) and _bind_carried_approval(
        repository, approvals, issue
    ):
        repository.add_label(number, TECH_LEAD_PROPOSAL_LABEL)
        repository.add_comment(
            number,
            "## 🔁 Approval carried over\n\n"
            f"@{removal.actor_login} approved this proposal under the old model"
            f" (by removing `{LEGACY_GATE_LABEL}`). It is now labelled"
            f" `{APPROVED_LABEL}`; the engine re-validates and acts on it once.",
        )
        return True
    repository.add_label(number, TECH_LEAD_PROPOSAL_LABEL)
    repository.add_label(number, AWAITING_APPROVAL_LABEL)
    who = (
        f"@{removal.actor_login}, who is not a maintainer or is automation,"
        if removal is not None
        else "something with no event on record"
    )
    repository.add_comment(
        number,
        "## ⏸️ Awaiting approval again\n\n"
        f"`{LEGACY_GATE_LABEL}` was removed by {who}, so it does not count as"
        " approval. This proposal waits for a maintainer's approval.",
    )
    return False


def _bind_carried_approval(
    repository: "RepositoryHost", approvals: "TechLeadApprovals", issue: "Issue"
) -> bool:
    """Make the maintainer's old-model approval an `approved` that verifies.

    The engine applies the label (unless one is already there) and binds the
    resulting event: its OWN write is recorded as the approval; a maintainer's
    own `approved` counts by itself. Anything else is not bound (False).
    """
    if not _carries(issue, APPROVED_LABEL):
        repository.add_label(issue.number, APPROVED_LABEL)
    event = approvals.evidence.latest_label_event(issue.number, APPROVED_LABEL)
    if event is None:
        raise RuntimeError(
            f"applied {APPROVED_LABEL!r} to #{issue.number} but no labeled event is on record"
        )
    if approvals.evidence.is_own_write(event):
        approvals.records.record_operator_approval(
            OperatorApprovalRecord(issue.number, event.event_id, datetime.now(timezone.utc).isoformat())
        )
        return True
    return approvals.is_maintainer(event)


__all__ = [
    "ApprovalMigrationError",
    "ApprovalMigrationReport",
    "LEGACY_GATE_LABEL",
    "migrate_legacy_proposals",
]
