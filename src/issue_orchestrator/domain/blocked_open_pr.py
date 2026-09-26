"""Open PRs whose review/rework the orchestrator keeps skipping (#7294).

When an issue (or its PR) carries a blocking label, the PR scanner still sees
the PR on every scan -- it is listed by ``needs-code-review`` or
``needs-rework`` -- and drops it: review validity says ``issue_blocked`` /
``pr_blocked``, the rework scan says the same. Before #7294 that decision
reached only a log line ("Skipping stale review PR ... reason=issue_blocked"),
so a ``blocked-failed`` issue whose finished, CI-green PR sat waiting on a
review io would never run was invisible to every health review: eight such
PRs waited on porchpin on 2026-09-23 until an operator noticed drafts in the
UI (porchpin#382).

This module is the owner of that fact. The scanner reports one
:class:`BlockedOpenPRObservation` per PR it skipped for a blocking label; the
:class:`BlockedOpenPRLedger` on ``OrchestratorState`` folds successive scans
into a skip count, and the board snapshot projects the ledger read-only. No
GitHub call is added: every fact here is one the scanner already fetched.

The ledger is in-memory, like the queues beside it on the state. After a
restart the first scan re-establishes it, so ``skip_count`` counts
consecutive scans observed by THIS orchestrator process, never more.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BlockedPRLane(StrEnum):
    """Which PR scan skipped the PR: the one that queues reviews or reworks."""

    REVIEW = "review"
    REWORK = "rework"


class BlockedPRSkipReason(StrEnum):
    """Where the blocking label that made the scanner skip the PR sits.

    The values are the ``reason=`` strings the scanner logs, so an entry can be
    matched against ``orchestrator.log`` directly.
    """

    ISSUE_BLOCKED = "issue_blocked"
    PR_BLOCKED = "pr_blocked"


@dataclass(frozen=True)
class BlockedOpenPRObservation:
    """One scan's fact: this open PR was skipped because of a blocking label.

    ``draft`` is ``None`` when the PR listing did not report it (the search
    fallback of the label listing), never guessed. ``issue_title`` is ``None``
    when the scan did not read the linked issue (it does not fetch one just
    for its title).
    """

    lane: BlockedPRLane
    issue_number: int
    issue_title: str | None
    pr_number: int
    pr_url: str
    draft: bool | None
    skip_reason: BlockedPRSkipReason
    blocking_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.blocking_labels:
            raise ValueError(
                f"PR #{self.pr_number} was recorded as blocked with no blocking label"
            )

    @property
    def key(self) -> tuple[BlockedPRLane, int]:
        """A PR is counted once per lane: it can carry both scan labels."""
        return (self.lane, self.pr_number)


@dataclass(frozen=True)
class BlockedOpenPR:
    """A ledger entry: the latest observation plus how long it has persisted.

    ``skip_count`` is the number of consecutive scans that skipped the PR in
    this lane; ``first_skipped_at``/``last_skipped_at`` are ISO timestamps of
    the first and latest of them.
    """

    observation: BlockedOpenPRObservation
    skip_count: int
    first_skipped_at: str
    last_skipped_at: str


class BlockedOpenPRLedger:
    """Owner of which open PRs the scanner is skipping for a blocking label.

    Each lane's scan is a complete statement about that lane: a PR it no longer
    reports has been unblocked, merged, closed or relabelled, so its entry --
    and its count -- ends there. A lane that was not scanned keeps its entries,
    because a throttled scan says nothing new.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[BlockedPRLane, int], BlockedOpenPR] = {}

    def record_scan(
        self,
        lane: BlockedPRLane,
        observations: Iterable[BlockedOpenPRObservation],
        *,
        at: datetime,
    ) -> None:
        """Replace ``lane``'s entries with this scan's observations.

        A PR that was already skipped in the previous scan of this lane keeps
        its ``first_skipped_at`` and has its count incremented.

        Raises:
            ValueError: an observation belongs to another lane, or the scan
                reported the same PR twice.
        """
        stamp = at.isoformat()
        scanned: dict[tuple[BlockedPRLane, int], BlockedOpenPR] = {}
        for observation in observations:
            if observation.lane is not lane:
                raise ValueError(
                    f"a {lane.value} scan reported a {observation.lane.value} "
                    f"observation for PR #{observation.pr_number}"
                )
            if observation.key in scanned:
                raise ValueError(
                    f"a {lane.value} scan reported PR #{observation.pr_number} twice"
                )
            previous = self._entries.get(observation.key)
            scanned[observation.key] = BlockedOpenPR(
                observation=observation,
                skip_count=previous.skip_count + 1 if previous else 1,
                first_skipped_at=previous.first_skipped_at if previous else stamp,
                last_skipped_at=stamp,
            )
        self._entries = {
            key: entry for key, entry in self._entries.items() if key[0] is not lane
        }
        self._entries.update(scanned)

    def entries(self) -> tuple[BlockedOpenPR, ...]:
        """Every entry, ordered by issue, then PR, then lane."""
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda entry: (
                    entry.observation.issue_number,
                    entry.observation.pr_number,
                    entry.observation.lane.value,
                ),
            )
        )
