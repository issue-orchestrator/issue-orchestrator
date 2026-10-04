"""What a PR whose merge is held for a person looks like now (#7763).

The Tech lead page lists PRs the engine escalated out of its merge lifecycle
(branch protection, stalled checks, a rejected merge queue entry). The cause
row says WHY the engine stopped; the operator also needs the PR's current
mergeability and checks, which only GitHub knows. This owner reads them with
the same discipline as the awaiting-merge reconciler: ``get_pr`` for state,
labels and ``mergeable_state``, and the status-check rollup only when
mergeability alone cannot say whether the checks are the problem.

The page refreshes every 30 seconds, so answers are reused for
:data:`MERGE_HOLD_STATUS_TTL_SECONDS`: a handful of held PRs costs a few reads
every few minutes, not per refresh. A read that fails is reported as such on
the card; it never hides the row.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PullRequestTracker

logger = logging.getLogger(__name__)

MERGE_HOLD_STATUS_TTL_SECONDS = 300.0

#: ``mergeable_state`` values for which the checks rollup is worth reading:
#: the PR is otherwise mergeable, so its checks decide.
_ROLLUP_STATES = frozenset({"unstable", "blocked"})


@dataclass(frozen=True)
class MergeHoldStatus:
    """One held PR, as GitHub reported it at ``read_at``."""

    pr_number: int
    title: str
    held: bool
    """Open and still carrying the needs-human label: a cause row whose PR
    was merged, closed or released is history, not something waiting."""
    mergeability: str
    checks: str
    read_error: str = ""


@dataclass
class MergeHoldStatuses:
    """Cached GitHub reads for the page's merge-held PRs."""

    host: "PullRequestTracker"
    needs_human_label: str
    clock: Callable[[], float] = time.monotonic
    ttl_seconds: float = MERGE_HOLD_STATUS_TTL_SECONDS
    _cache: dict[int, tuple[float, MergeHoldStatus]] = field(default_factory=dict, init=False)

    def read(self, pr_numbers: Iterable[int]) -> dict[int, MergeHoldStatus]:
        now = self.clock()
        wanted = set(pr_numbers)
        for stale in set(self._cache) - wanted:
            self._cache.pop(stale)
        statuses: dict[int, MergeHoldStatus] = {}
        for number in sorted(wanted):
            cached = self._cache.get(number)
            if cached is None or now - cached[0] >= self.ttl_seconds:
                cached = (now, self._read_one(number))
                self._cache[number] = cached
            statuses[number] = cached[1]
        return statuses

    def _read_one(self, number: int) -> MergeHoldStatus:
        try:
            pr = self.host.get_pr(number)
        except Exception as error:
            logger.warning("[tech_lead page] could not read held PR #%d: %s", number, error)
            return MergeHoldStatus(number, f"#{number}", True, "unknown", "unknown", read_error=str(error))
        if pr is None:
            return MergeHoldStatus(number, f"#{number}", False, "missing", "unknown")
        held = pr.state == "open" and any(
            str(label).casefold() == self.needs_human_label.casefold() for label in pr.labels
        )
        mergeability = pr.mergeable_state or "unknown"
        checks = "not read"
        if held and mergeability in _ROLLUP_STATES:
            try:
                rollup = self.host.read_pr_status_check_rollup(number)
            except Exception as error:
                logger.warning("[tech_lead page] could not read checks of PR #%d: %s", number, error)
                checks = f"unreadable: {error}"
            else:
                checks = (rollup.state or "no checks") if rollup.capability == "ok" else rollup.capability
        elif held:
            checks = "not decisive (mergeability says it all)"
        return MergeHoldStatus(number, pr.title, held, mergeability, checks)


__all__ = ["MERGE_HOLD_STATUS_TTL_SECONDS", "MergeHoldStatus", "MergeHoldStatuses"]
