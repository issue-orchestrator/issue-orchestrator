"""Durable disposition of a completed failure investigation (#6971).

A failure investigation that SUCCEEDS — diagnosis posted, pattern accrued,
follow-ups filed — used to leave nothing machine-readable behind. The blocking
label stayed, so the next stuck sweep re-discovered the issue "still stuck and
NOT owned", spent a unit of recovery budget, and queued another investigation
that re-read the same evidence and reached the same conclusion. Issue #6410 was
investigated three times for one verdict; #6410/#6411 each had ~2 more coming.

The gap was in the vocabulary, not the sweep: no tech-lead decision action could
say "diagnosed; the remedy is owned elsewhere". ``escalate_to_human`` applies a
bare ``needs-human`` label, which ``stuck_sweep._reconciler_owns`` deliberately
leaves sweep-ELIGIBLE, and ``reset_retry`` is exactly wrong for the common case
(it discards validated work). So the sweep kept re-asking a question that had
already been answered.

This module owns the missing disposition end to end:

* :func:`disposition_comment` — the wait state published on the diagnosed
  issue, rendered in one place so the comment and the row can never describe
  different bindings.
* :func:`apply_record_tech_lead_disposition` — the apply-time owner of the
  durable tracker binding a ``defer_to_tracker`` decision action records.
* :class:`TechLeadDispositionLedger` — the sweep-time owner. It answers "which
  issues does a live disposition own?" and RELEASES rows whose binding no
  longer holds, so a disposition can never park an issue forever.

**The binding is the release condition.** A disposition names an OPEN tracker.
While that tracker is open the diagnosed issue is owned (the way a
``tech-lead-needs-human`` marker makes an issue owned) and the sweep leaves its
budget alone. The moment the tracker closes — or vanishes, or was never real —
the wait state is over: the row is released and the issue goes straight back to
the sweep for a fresh look. A recovered issue (its blocking label cleared)
releases for the same reason.

**Ledger, not label.** The tracker number is the load-bearing half of this
state and no label can carry it, so the authority store is the single source of
truth rather than a label plus a store that must be reconciled with it. The
operator-facing surface is the comment this posts on the diagnosed issue, which
names the tracker and says exactly why the sweep will leave the issue alone.
Losing the store degrades to the OLD behaviour (one redundant investigation),
never to a silently-parked issue.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Protocol

from .action_results import ActionResult
from .tech_lead_actions import RecordTechLeadDispositionAction

if TYPE_CHECKING:
    from ..domain.tech_lead_session import TechLeadDisposition
    from ..ports import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .actions import Action

logger = logging.getLogger(__name__)

#: ``(issue_number) -> "open" | "closed" | None``. ``None`` means the issue does
#: not exist — treated exactly like closed, because a disposition bound to an
#: issue that is not there has nothing left to wait for.
IssueStateReader = Callable[[int], "str | None"]


def disposition_comment(disposition: "TechLeadDisposition") -> str:
    """The operator-facing wait state published on the diagnosed issue."""
    return (
        "## 🅿️ Tech Lead disposition — diagnosed, awaiting recovery\n\n"
        f"This issue's failure is diagnosed. Its remedy is owned by"
        f" #{disposition.tracker_issue_number}, so the orchestrator will NOT"
        " re-open a failure investigation for it while that tracker is open —"
        " re-diagnosing an answered question spends recovery budget that"
        " undiagnosed issues need.\n\n"
        f"{disposition.rationale}\n\n"
        f"The stuck sweep resumes examining this issue when"
        f" #{disposition.tracker_issue_number} closes with this issue still"
        " blocked, or when its blocking label clears."
        f"\n\n---\n*Recorded by tech_lead session (action"
        f" {disposition.source_action_id}) — ADR-0031, #6971.*"
    )


def apply_record_tech_lead_disposition(
    action: "Action",
    *,
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Record the durable tracker binding for a diagnosed issue (#6971).

    Ledger-only, on purpose. The wait-state COMMENT that explains the parking
    is planned as an ordinary :class:`~.actions.AddCommentAction` ahead of this
    one, so it crosses the applier's claim-verified comment handler like every
    other write to a real work issue — the same delegation shape the reset and
    kill owners use. This half writes nothing to GitHub, so it needs no claim
    check of its own; the expected-state gate still runs, because the registry
    wraps every mutating tech-lead command in it and this command names the
    diagnosed issue as its reconciliation subject.
    """
    assert isinstance(action, RecordTechLeadDispositionAction)
    if authority is None:
        return ActionResult.fail(
            action,
            "recording a tech-lead disposition requires the"
            " TechLeadAuthorityStore wired into this applier",
        )
    disposition = action.disposition
    assert disposition is not None  # enforced by the action's __post_init__
    try:
        authority.record_disposition(disposition=disposition)
    except Exception as exc:
        logger.exception(
            "Failed to record tech-lead disposition for issue #%d (tracker #%d)",
            disposition.issue_number,
            disposition.tracker_issue_number,
        )
        return ActionResult.fail(action, str(exc))
    return ActionResult.ok(
        action,
        issue_number=disposition.issue_number,
        tracker_issue_number=disposition.tracker_issue_number,
    )


class StuckSweepDispositions(Protocol):
    """What the stuck sweep needs from the disposition ledger (#6971).

    A behaviour-level seam, not a store handle: the sweep asks who is owned and
    reports who recovered, and never learns that dispositions are rows, that a
    tracker's state comes from GitHub, or when a row is discarded.
    """

    def owned_issue_numbers(self) -> frozenset[int]:
        """Issues a live disposition owns; releases bindings that lapsed."""
        ...

    def release(self, issue_numbers: frozenset[int]) -> None:
        """Drop dispositions for issues that recovered (no longer blocked)."""
        ...


class _NoDispositions:
    """Null ledger for a sweep running without an authority store."""

    def owned_issue_numbers(self) -> frozenset[int]:
        return frozenset()

    def release(self, issue_numbers: frozenset[int]) -> None:
        return None


#: The sweep's default: no store wired, so no issue is disposition-owned.
NO_TECH_LEAD_DISPOSITIONS: StuckSweepDispositions = _NoDispositions()


class TechLeadDispositionLedger:
    """Owner of "is this issue's disposition still binding?" (#6971).

    Reads are bounded and rare by construction: the ledger holds one row per
    diagnosed-and-parked issue (a handful), the sweep is the only caller, and
    the sweep is timer-gated — so a not-due sweep costs ZERO GitHub calls, and a
    due one costs at most one issue-state read per DISTINCT bound tracker.
    """

    def __init__(
        self,
        *,
        authority: "TechLeadAuthorityStore",
        issue_state: IssueStateReader,
    ) -> None:
        self._authority = authority
        self._issue_state = issue_state

    def owned_issue_numbers(self) -> frozenset[int]:
        """Issues whose disposition still binds, releasing the ones that lapsed.

        A tracker that is closed, missing, or unreadable ends the wait state.
        Unreadable is treated as lapsed on purpose: failing OPEN here would let
        one flaky read park an issue indefinitely, whereas failing closed costs
        at most one redundant investigation — the pre-#6971 behaviour.
        """
        owned: set[int] = set()
        tracker_open: dict[int, bool] = {}
        for disposition in self._authority.list_dispositions():
            tracker = disposition.tracker_issue_number
            if tracker not in tracker_open:
                tracker_open[tracker] = self._tracker_is_open(tracker)
            if tracker_open[tracker]:
                owned.add(disposition.issue_number)
                continue
            logger.info(
                "[STUCK_SWEEP] releasing disposition for issue #%d: its"
                " recovery tracker #%d is no longer open (#6971)",
                disposition.issue_number,
                tracker,
            )
            self._authority.discard_disposition(issue_number=disposition.issue_number)
        return frozenset(owned)

    def release(self, issue_numbers: frozenset[int]) -> None:
        """Drop the dispositions of issues that recovered.

        Mirrors the sweep's own recovery-budget reset: an issue that no longer
        carries a blocking label has recovered, so a stale row must not survive
        to park a LATER, unrelated incident on the same number.
        """
        for issue_number in sorted(issue_numbers):
            if self._authority.load_disposition(issue_number=issue_number) is None:
                continue
            logger.info(
                "[STUCK_SWEEP] releasing disposition for issue #%d: it is no"
                " longer blocked (#6971)",
                issue_number,
            )
            self._authority.discard_disposition(issue_number=issue_number)

    def _tracker_is_open(self, tracker_issue_number: int) -> bool:
        try:
            return self._issue_state(tracker_issue_number) == "open"
        except Exception:
            logger.warning(
                "[STUCK_SWEEP] could not read the state of recovery tracker"
                " #%d; treating the disposition it backs as lapsed (#6971)",
                tracker_issue_number,
                exc_info=True,
            )
            return False


def build_disposition_ledger(
    authority: "TechLeadAuthorityStore | None",
    repository_host: "RepositoryHost",
) -> StuckSweepDispositions:
    """The sweep's disposition owner, or the null ledger without a store."""
    if authority is None:
        return NO_TECH_LEAD_DISPOSITIONS
    return TechLeadDispositionLedger(
        authority=authority, issue_state=repository_host.get_issue_state
    )
