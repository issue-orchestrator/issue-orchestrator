"""Startup migration of the legacy ``proposed-tech-lead`` gate (#7763).

The approval model replaced "remove ``proposed-tech-lead`` to approve" with a
positive, maintainer-applied ``approved`` label. Two kinds of open proposal
predate it:

1. **Still gated** — open and carrying ``proposed-tech-lead``. They move to the
   new labels with no behaviour change: the proposal body marker is written,
   provenance and waiting state are added, then the legacy label comes off (in
   that order, so a crash between the writes leaves the item gated and the next
   startup finishes the move).
2. **Approved under the old model but not yet executed** — an open proposal
   with a stored op and none of the approval labels: someone removed the
   legacy gate. The removal is honoured only if a MAINTAINER made it (the
   latest ``unlabeled`` event's actor): the engine applies ``approved`` (before
   provenance, so a crash resumes) and records that exact label event as the
   maintainer's approval, so the next tick executes it once. Any other remover
   (a bot, a retry, an agent) re-gates the item and says so.
3. **Already on the new labels without the body marker** — marked.

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
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, TypeVar

from ..domain.tech_lead_approval import (
    APPROVED_LABEL,
    AWAITING_APPROVAL_LABEL,
    TECH_LEAD_PROPOSAL_LABEL,
    OperatorApprovalRecord,
    carries_proposal_marker,
    labels_named,
    missing_labels,
    with_proposal_marker,
)
from .tech_lead_approval_writes import restore_gate_labels
from .tech_lead_proposals import TECH_LEAD_PROPOSAL_SCAN_LIMIT

if TYPE_CHECKING:
    from ..infra.config import Config
    from .action_applier import ActionApplier
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_approval import TechLeadApprovals

_Item = TypeVar("_Item", "Issue", int)

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


def migrate_engine_proposals(
    repository: "RepositoryHost",
    applier: "ActionApplier | None",
    ops: "TechLeadAuthorityStore | None",
    config: "Config",
) -> ApprovalMigrationReport | None:
    """Startup's step (#7763): migrate when this engine has an approval owner
    and an authority store; fail-fast, since an unmigrated legacy gate no
    longer blocks anything."""
    approvals = applier.tech_lead_approvals if applier is not None else None
    if approvals is None or ops is None:
        return None
    return migrate_legacy_proposals(repository, approvals, ops, filtering_label=config.filtering.label)


def migrate_legacy_proposals(
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals",
    ops: "TechLeadAuthorityStore",
    *,
    filtering_label: str | None,
) -> ApprovalMigrationReport:
    """Move every open legacy proposal IN THIS ENGINE'S SCOPE onto the approval
    model (idempotent). ``filtering_label`` is the engine's scope label: an
    engine scoped to one label (an e2e run, a shared repository) never touches
    another engine's proposals."""
    errors: list[str] = []
    regated = _each(
        _open_with(repository, LEGACY_GATE_LABEL, filtering_label), errors, "Migrating legacy proposal",
        lambda issue: _regate(repository, issue) or True,
    )
    # Proposals already on the new labels but filed before the body marker
    # existed (or migrated by a run that crashed before marking them).
    marked = _each(
        _open_with(repository, TECH_LEAD_PROPOSAL_LABEL, filtering_label), errors, "Marking proposal body",
        lambda issue: _mark_body(repository, issue) or True,
    )
    outcomes = _each(
        [number for number, _op in ops.list_ops() if number not in regated], errors,
        "Migrating op-backed proposal",
        lambda number: _migrate_legacy_approval(repository, approvals, number, filtering_label),
    )
    # Seed the approval scope's index (#7763 review r6 F2): a later strip of
    # every gate label must not hide any of these from the inbox. The marker
    # sweep also recovers a proposal filed but never indexed — the engine died
    # between GitHub's create and the index write (#7763 review r7 F3).
    _sweep_proposal_markers(repository, approvals, filtering_label, errors)
    approvals.remember_proposals({*regated, *marked, *outcomes})
    report = ApprovalMigrationReport(
        tuple(regated),
        tuple(n for n, outcome in outcomes.items() if outcome is True),
        tuple(n for n, outcome in outcomes.items() if outcome is False),
        tuple(errors),
    )
    if report.errors:
        # Fail fast: the legacy label no longer blocks anything, so a proposal
        # left unmigrated would be schedulable. Everything migratable was
        # migrated above; the next startup retries the rest.
        raise ApprovalMigrationError(report)
    if report.changed:
        logger.info(
            "[tech_lead] Approval label migration: regated=%s legacy_approved=%s"
            " legacy_unapproved=%s",
            report.regated,
            report.legacy_approved,
            report.legacy_unapproved,
        )
    return report


#: The marker sweep reads every open issue in scope once per startup.
_MARKER_SWEEP_LIMIT = 10_000


def _sweep_proposal_markers(
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals",
    filtering_label: str | None,
    errors: list[str],
) -> None:
    """Index every open in-scope issue whose body carries the proposal marker."""
    from .health_review_trigger import _scoped_issues

    try:
        issues = repository.list_issues(
            labels=[filtering_label] if filtering_label else [],
            state="open",
            limit=_MARKER_SWEEP_LIMIT,
            exhaustive=True,
        )
        approvals.remember_proposals(
            issue.number
            for issue in _scoped_issues(issues, filtering_label)
            if carries_proposal_marker(issue.body)
        )
    except Exception as exc:  # the rest of the migration still runs
        logger.exception("[tech_lead] Sweeping proposal markers failed")
        errors.append(f"marker sweep: {exc}")


def _open_with(repository: "RepositoryHost", label: str, filtering_label: str | None) -> list["Issue"]:
    from .health_review_trigger import _scoped_issues

    issues = repository.list_issues(
        labels=[value for value in (label, filtering_label) if value],
        state="open",
        limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        exhaustive=True,
    )
    return _scoped_issues(issues, filtering_label)


def _each(
    items: "Iterable[_Item]",
    errors: list[str],
    what: str,
    step: "Callable[[_Item], bool | None]",
) -> dict[int, bool | None]:
    """Run *step* on every item; one failure never stops the others.

    Returns ``{number: outcome}`` for the items whose step returned a verdict.
    """
    outcomes: dict[int, bool | None] = {}
    for item in items:
        number = item if isinstance(item, int) else item.number
        try:
            outcome = step(item)
        except Exception as exc:  # migrate the rest, then fail the startup
            logger.exception("[tech_lead] %s #%d failed", what, number)
            errors.append(f"#{number}: {exc}")
            continue
        if outcome is not None:
            outcomes[number] = outcome
    return outcomes


def _mark_body(repository: "RepositoryHost", issue: "Issue") -> None:
    """Write the proposal body marker, so a later full label strip cannot turn
    the migrated proposal into ordinary work (#7763 review r2 F1)."""
    if not carries_proposal_marker(issue.body):
        repository.update_issue_body(issue.number, with_proposal_marker(issue.body or ""))


def _regate(repository: "RepositoryHost", issue: "Issue") -> None:
    # Marker first: until the legacy label is gone it is the legacy label that
    # gates, so each later write leaves the item gated if the next one fails.
    _mark_body(repository, issue)
    restore_gate_labels(repository, issue)
    for label in labels_named(issue.labels, LEGACY_GATE_LABEL):
        repository.remove_label(issue.number, label)


def _migrate_legacy_approval(
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals",
    number: int,
    filtering_label: str | None,
) -> bool | None:
    """None: nothing to migrate. True: honoured as approved. False: re-gated.

    The body marker is the record that this item was migrated (#7763 review
    r5 F1): once present, the approval model owns the item, so an item whose
    approval labels were stripped AFTER migration is a stripped proposal (the
    engine re-gates it), never an old-model approval to carry again.

    Resumable without a journal: on the approval path the label is written
    and bound FIRST, then the marker, then provenance. A crash before the
    marker redoes the step on the next startup; a crash after it leaves a
    marked proposal whose gate labels the next startup restores, after which
    its bound approval verifies.
    """
    issue = repository.get_issue(number)
    if issue is None or issue.state != "open" or _carries(issue, LEGACY_GATE_LABEL):
        return None
    if filtering_label and not _carries(issue, filtering_label):
        return None  # another engine's proposal (a shared authority store)
    if _carries(issue, TECH_LEAD_PROPOSAL_LABEL) or _carries(issue, AWAITING_APPROVAL_LABEL):
        return None
    if carries_proposal_marker(issue.body):
        # Migrated already: the approval model owns it now. Its approval is
        # never carried again (a strip after migration revoked it), but a
        # crash between the marker and the gate labels, or a later strip of
        # them, must not leave it outside every label query (#7763 r6 F1).
        restore_gate_labels(repository, issue)
        approvals.remember_proposals([number])
        return None
    removal = approvals.evidence.latest_label_event(number, LEGACY_GATE_LABEL, removed=True)
    if removal is not None and approvals.is_maintainer(removal) and _bind_carried_approval(
        repository, approvals, issue
    ):
        _mark_body(repository, issue)
        repository.add_label(number, TECH_LEAD_PROPOSAL_LABEL)
        repository.add_comment(
            number,
            "## 🔁 Approval carried over\n\n"
            f"@{removal.actor_login} approved this proposal under the old model"
            f" (by removing `{LEGACY_GATE_LABEL}`). It is now labelled"
            f" `{APPROVED_LABEL}`; the engine re-validates and acts on it once.",
        )
        return True
    _mark_body(repository, issue)
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
    for label in missing_labels(issue.labels, (APPROVED_LABEL,)):
        repository.add_label(issue.number, label)
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
