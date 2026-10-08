"""Which episode of the shared needs-human block each blocked item is in (#8688).

A blocked-item triage covers the block EPISODE it was decided on. The block's
one owner (:class:`~.needs_human_block.NeedsHumanBlock`) opens a generation when
it puts the label on afresh and ends it when the label comes off, but it sees
only its own writes. GitHub sees every write. This owner binds each generation
to GitHub's standing ``labeled`` event (the approval owner's complete
issue-event reader, #7763):

* a health review's agenda verifies every item before granting any, so every
  triage is made on an episode bound to the label application standing then;
* the tick's "is a triage owed" check reads the recorded episodes, and
  re-verifies them at most once per health-review interval, so a label taken
  off and put back by hand, unseen by the owner, still makes a review due
  without a GitHub event scan on every tick.

An item whose events cannot be read, that GitHub does not show the label
standing on, or whose block is being changed by its owner right now (the
owner's per-issue gate is busy) has an UNKNOWN episode. Its triage is owed and
never covered: the rule fails toward triaging again, never toward silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ..domain.issue_disposition_gate import IssueDispositionGateStatus

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import StandingLabel
    from ..ports.pending_work_claim_store import NeedsHumanEpisodeReader
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class NeedsHumanEpisodes:
    """THE reader of needs-human block episodes for the triage rule (#8688)."""

    def __init__(
        self,
        *,
        store: "NeedsHumanEpisodeReader",
        label_applications: Callable[[int, str], "StandingLabel | None"],
        labels: "LabelManager",
        clock: Callable[[], float],
        recheck_seconds: float,
    ) -> None:
        self._store = store
        self._label_applications = label_applications
        self._labels = labels
        self._clock = clock
        self._recheck_seconds = recheck_seconds
        self._checked_at: float | None = None

    def verified(self, issues: Mapping[int, Sequence[str]]) -> dict[int, str]:
        """Each item's episode, bound to GitHub's standing label application.

        ``issues`` maps each item to its labels. An item whose episode cannot
        be verified is left out: its episode is unknown.
        """
        verified: dict[int, str] = {}
        for number, labels in issues.items():
            with self._store.mutate_needs_human(number) as status:
                if status is IssueDispositionGateStatus.BUSY:
                    continue  # the owner is changing this block right now
                episode = self._bind(number, self._episode_label(labels))
            if episode is not None:
                verified[number] = episode
        return verified

    def current(self, issues: Mapping[int, Sequence[str]]) -> dict[int, str]:
        """The recorded episodes, re-verified once per recheck period.

        The tick reads this, so it makes no GitHub read between rechecks. A
        recheck that finds a new application opens a new episode, and the
        triage made on the old one no longer covers the item.
        """
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._recheck_seconds:
            self._checked_at = now
            return self.verified(issues)
        return self._store.needs_human_episodes(sorted(issues))

    def _bind(self, number: int, label: str) -> str | None:
        try:
            standing = self._label_applications(number, label)
        except Exception:
            logger.warning(
                "[TRIAGE] %s events of #%d unreadable; its block episode is unverified",
                label, number, exc_info=True,
            )
            return None
        if standing is None:
            return None  # GitHub does not show it standing: the cache is stale
        application = standing.application
        return self._store.bind_needs_human_episode(
            number, event_id=application.event_id, applied_at=application.created_at,
        )

    def _episode_label(self, issue_labels: Sequence[str]) -> str:
        """The label whose application dates the episode: needs-human, else the marker."""
        needs_human = self._labels.needs_human
        if needs_human.casefold() in {label.casefold() for label in issue_labels}:
            return needs_human
        return self._labels.tech_lead_needs_human


class _UnwiredEpisodes(NeedsHumanEpisodes):
    """Stands in until the composition binds the real owner: reading raises."""

    def __init__(self) -> None:
        pass

    def verified(self, issues: Mapping[int, Sequence[str]]) -> dict[int, str]:
        raise RuntimeError("needs-human episodes are not wired (#8688)")

    def current(self, issues: Mapping[int, Sequence[str]]) -> dict[int, str]:
        raise RuntimeError("needs-human episodes are not wired (#8688)")


UNWIRED_EPISODES: NeedsHumanEpisodes = _UnwiredEpisodes()
