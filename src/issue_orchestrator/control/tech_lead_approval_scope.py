"""The approval backlog's own observation scope (#7014).

Split from ``tech_lead_proposals`` because it is one subject with one rule:
the backlog is defined by a LABEL, so only a query for that label observes it,
and only that query may decide who is in it. Everything else a tick holds is a
query for something else that merely overlaps.

Kept out of the fact gatherer for the same reason it is kept out of the
proposal lifecycle: deciding WHEN to re-observe and WHAT counts as observed is
policy about the approval scope, not about gathering facts or reconciling ops.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.tech_lead_approval import (
    AWAITING_APPROVAL_LABEL,
    TECH_LEAD_PROPOSAL_LABEL,
)
from ..domain.tech_lead_session import GatedTechLeadProposal
from .tech_lead_proposals import (
    TECH_LEAD_PROPOSAL_SCAN_LIMIT,
    observe_gated_tech_lead_proposals,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from .tech_lead_approval import TechLeadApprovals

# How often the approval scope is re-observed when nothing else arms the tick.
# One labelled, exhaustive read per interval rather than per tick, while still
# guaranteeing a repo with only hidden approvals discovers them.
APPROVAL_SCAN_INTERVAL_SECONDS = 300.0

logger = logging.getLogger(__name__)


def approval_refresh_due(
    config: "Config", state: "OrchestratorState", now: float, authority: object
) -> bool:
    """Whether the approval scope must be re-observed on this tick.

    Deliberately independent of every other trigger. Each of those is either
    unrelated to approvals or reads the worker board — the very fetch whose
    blind spot the gate query exists to cover — so arming on them infers
    "quiet" from the source that cannot see the thing being looked for. A repo
    whose only tech-lead activity is worker-labelled proposals outside that
    board therefore never armed, never queried, and never showed them.

    ``tech_lead_approval_scan_at`` is 0.0 on a fresh state, so the first tick
    after startup is always due. Gated on an authority store because without
    one there is no proposal lifecycle at all, and a repo in that shape keeps
    costing nothing.
    """
    if not config.tech_lead_enabled or authority is None:
        return False
    return now - state.tech_lead_approval_scan_at >= APPROVAL_SCAN_INTERVAL_SECONDS


def discover_open_gated_proposals(
    repository_host: "RepositoryHost",
    config: "Config",
    indexed: Collection[int] = frozenset(),
) -> tuple[list["Issue"], tuple[int, ...]]:
    """AUTHORITATIVE observation of every open proposal, in its own scope.

    Proposals are defined by their approval LABELS (#7763), so the only
    complete observation of them is a query for each: the provenance label
    (the unapproved backlog AND the approved items the approval owner must
    verify — a claimed approval, or an admitted follow-up still being worked)
    and the waiting label (a proposal whose provenance label was stripped).
    The two answers are unioned by issue number. Everything the tick already
    holds is a query for something else that merely overlaps:

    - the worker board is narrowed by configured agents, milestones, exclusion
      filters and a fetch limit — it fetches runnable work, not approvals;
    - the anchor scan queries the TECH-LEAD agent label, while a promoted
      finding carries the TARGET'S worker agent label so it is
      "DISCOVERABLE the moment the gate comes off"
      (:func:`~.tech_lead_finding_promotion.promotion_issue_labels`) — and is
      therefore structurally invisible to an agent-scoped scan.

    Joining those two does not produce a complete set; it produces two
    incomplete ones. This costs one labelled query on ticks that already do
    tech-lead work, and it is what lets the board be written straight from
    the facts: a complete observation needs no retention, and retention is
    what would let an approved proposal linger (the warning in
    ``_build_view``'s own docstring).

    ``exhaustive`` for the same reason the anchor scan is (#6779 R17): a
    dropped page must RAISE rather than return a silently partial set a caller
    would read as "fewer approvals pending".

    Labels alone are not the whole scope (#7763 review r6 F2): a bulk edit can
    strip every approval label from a proposal whose body marker still blocks
    it. The *indexed* proposals (every one this engine filed or migrated) that
    neither label query returned are read one by one — normally none, since
    a proposal carries its labels — and returned with the scope when still an
    open, in-scope proposal. The second element names the indexed numbers
    found closed, gone or no longer a proposal, for the index to retire.
    """
    from .health_review_trigger import _scoped_issues

    found: dict[int, "Issue"] = {}
    for gate_label in (TECH_LEAD_PROPOSAL_LABEL, AWAITING_APPROVAL_LABEL):
        for issue in repository_host.list_issues(
            labels=[value for value in (gate_label, config.filtering.label) if value],
            state="open",
            limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
            exhaustive=True,
        ):
            # The later query's snapshot wins (#7763 review r21 F1): it is
            # the newer read of the same issue.
            found[issue.number] = issue
    retired: list[int] = []
    for number in sorted(set(indexed) - set(found)):
        issue = repository_host.get_issue(number)
        # An open indexed issue stays in scope whatever its labels and body
        # say now (#7763 review r15 F1): only closing it retires it.
        if issue is None or issue.state != "open":
            retired.append(number)
        else:
            found[number] = issue
    scoped = _scoped_issues([found[number] for number in sorted(found)], config.filtering.label)
    return scoped, tuple(retired)


@dataclass(frozen=True)
class ApprovalScopeObservation:
    """One observation of the approval scope (#7763).

    ``backlog`` is what the board publishes (unapproved proposals);
    ``issues`` is the authoritative scope itself — every open proposal,
    approved or not — which the approval owner verifies and settles.
    """

    backlog: tuple[GatedTechLeadProposal, ...]
    issues: tuple["Issue", ...]
    retired: tuple[int, ...] = ()
    #: False when this tick reused the owner's last complete observation
    #: instead of querying GitHub (:func:`approval_scope_for_tick`).
    refreshed: bool = True


def observe_approval_backlog(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
) -> tuple[GatedTechLeadProposal, ...]:
    """The unapproved backlog alone; see :func:`observe_approval_scope`."""
    return observe_approval_scope(repository_host, config, *partial).backlog


def observe_approval_scope(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
    indexed: Collection[int] = frozenset(),
) -> ApprovalScopeObservation:
    """The backlog as the board should publish it: complete, and this tick's.

    Composes the two halves so no caller has to remember to do both. The sets
    a tick already holds go in first (free, and the freshest evidence about
    the issues they cover), and the authoritative gate-label query goes last
    so it decides the ones only it can see.

    The exhaustive query decides membership; the free sets only enrich it.
    Complete on purpose, because the board is written straight from the
    result. The alternative — publishing a partial observation and retaining
    what it missed — trades erasing a pending approval for advertising one
    the operator already approved, which is the failure ``_build_view``'s
    docstring warns about and #7014's own symptom.
    """
    authoritative, retired = discover_open_gated_proposals(repository_host, config, indexed)
    # MEMBERSHIP comes from the authoritative query; the partial sets may only
    # enrich what it already contains.
    #
    # Appending it to a union does not make it authoritative, because its
    # verdict on a retired proposal is ABSENCE, and absence supersedes nothing.
    # If the worker board saw gated #9000 and the operator approves it before
    # this query runs, the query returns no #9000 at all — so a plain union
    # keeps advertising it on the strength of the older, narrower observation,
    # against the complete later one. (That is distinct from an explicit
    # ungated or closed OBJECT arriving later, which the observer already
    # handles by resolving the latest observation per issue.)
    in_scope = {issue.number for issue in authoritative}
    observed = observe_gated_tech_lead_proposals(*partial, authoritative, known=indexed)
    return ApprovalScopeObservation(
        backlog=tuple(
            proposal for proposal in observed if proposal.issue_number in in_scope
        ),
        issues=tuple(authoritative),
        retired=retired,
    )


def observe_approval_scope_or_none(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
    decline_on_failure: bool = True,
    indexed: Collection[int] = frozenset(),
) -> ApprovalScopeObservation | None:
    """The backlog, or None when this tick could not observe its scope.

    ``decline_on_failure`` must be False whenever the tick has ALREADY gathered
    facts of its own — an anchor scan, approved ops, promotion updates. None
    reaches the planner as "nothing armed", which is not merely a missing
    board: with a problem storm underway it discards the very
    ``existing_health_review_issue`` this tick observed, and the storm planner
    mints a DUPLICATE anchor. Declining to publish is a statement about the
    approval display only; it must never be a statement about facts that were
    successfully observed (F5). Where those exist, the failure propagates so
    the snapshot cannot be planned at all, which is how the anchor scan has
    always behaved.

    None means DECLINE TO PUBLISH. The board is rewritten wholesale from the
    facts a tick produces, so publishing a backlog we could not observe is
    exactly the erasure this work exists to prevent — an operator would watch
    approvals vanish because GitHub was briefly unreachable. Leaving the last
    good board in place costs nothing the outage was not already costing, and
    cannot strand: the caller does not advance its refresh timestamp, so the
    next tick retries immediately.
    """
    from ..ports.repository_host import RepositoryHostError

    try:
        return observe_approval_scope(repository_host, config, *partial, indexed=indexed)
    except RepositoryHostError as error:
        if not decline_on_failure:
            raise
        logger.warning(
            "[tech_lead] approval scope unobservable this tick, board left as "
            "published: %s",
            error,
        )
        return None


def approval_scope_for_tick(
    repository_host: "RepositoryHost",
    config: "Config",
    approvals: "TechLeadApprovals | None",
    *partial: Sequence["Issue"],
    due: bool,
    decline_on_failure: bool,
) -> ApprovalScopeObservation | None:
    """This tick's approval scope: queried only when the cadence (or an
    operator's command) says so (#7763 review r21 F2).

    Other triggers (a pending op, say) arm fact production every tick; they
    reuse the approval owner's last COMPLETE observation, refreshed with the
    fresher sets this tick holds, instead of re-running the exhaustive gate
    queries. A refresh marks the scope unavailable first, so a failed one is
    never served as current (#7763 review r16 F2).
    """
    if not due and approvals is not None and approvals.scope_observed:
        retained = tuple(issue for issue, _verdict in approvals.observed_scope())
        in_scope = {issue.number for issue in retained}
        observed = observe_gated_tech_lead_proposals(
            retained, *partial, known=approvals.indexed_proposals()
        )
        return ApprovalScopeObservation(
            backlog=tuple(item for item in observed if item.issue_number in in_scope),
            issues=retained,
            refreshed=False,
        )
    if approvals is not None:
        approvals.mark_scope_unavailable()
    return observe_approval_scope_or_none(
        repository_host, config, *partial,
        decline_on_failure=decline_on_failure,
        indexed=approvals.indexed_proposals() if approvals is not None else frozenset(),
    )
