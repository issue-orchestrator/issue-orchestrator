"""Classify awaiting-merge label drift from an issue's associated PR set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo


@dataclass(frozen=True)
class PrSetDriftClassification:
    """Whether an issue's PR set indicates ``blocked:pr-closed`` drift.

    ``drifting`` is true when the issue should be flagged. ``pr`` is the PR the
    drift keys on, or ``None`` for the "no associated PR" case.
    """

    drifting: bool
    pr: PRInfo | None = None


def classify_pr_set_drift(prs: list[PRInfo]) -> PrSetDriftClassification:
    """Own the ``blocked:pr-closed`` precedence policy for a PR set.

    Policy:
    - Any open PR means the issue is still legitimately awaiting a merge.
    - No PRs means drift with no PR reference ("PR missing").
    - Otherwise, the latest terminal PR decides: merged suppresses drift;
      closed-unmerged produces drift keyed on that latest PR.
    """
    if any(_normalized_state(pr.state) == "open" for pr in prs):
        return PrSetDriftClassification(drifting=False)
    if not prs:
        return PrSetDriftClassification(drifting=True)
    latest = max(prs, key=lambda item: item.number)
    if latest.is_closed_unmerged:
        return PrSetDriftClassification(drifting=True, pr=latest)
    return PrSetDriftClassification(drifting=False)


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()
