"""Which episode of its block each blocked item is in (#8688, #8731).

A blocked-item triage covers the block EPISODE it was decided on, so a block
lifted and later re-raised under the same labels owes a new triage. A block's
episode is the onset of each of its parts:

* the shared needs-human block (its label, or the tech-lead hand-over marker):
  the generation its one owner records, bound to GitHub's standing label
  application by :class:`~.needs_human_episodes.NeedsHumanEpisodes` (#8688);
* every other blocking label (``blocked``, ``blocked-failed``,
  ``publish-failed``, ``recovery-pending``, ``blocked:*``,
  ``blocked-cross-milestone``, ...): GitHub's standing application of it
  (:class:`~..ports.label_application.LabelApplicationReader`, #8731). These
  labels have no single writer that could date an episode (the engine writes
  them from many paths, and an operator by hand), but GitHub records every
  write: the first ``labeled`` event of the label's standing run is its onset
  whoever put it on, and a removal and re-application is a new event. One read
  of the issue's events dates all of an item's labels.

This owner reads them on one schedule:

* a health review's agenda verifies every item (:meth:`BlockEpisodes.verified`)
  before granting any, so every triage is made on the episode standing then;
* the tick's "is a triage owed" check (:meth:`BlockEpisodes.current`) re-reads
  GitHub at most once per health-review interval and, between rechecks, reads
  what the last recheck verified, so a re-raised block makes a review due by
  the next recheck without an event scan on every tick.

A part whose onset cannot be verified (events unreadable, the label not
standing on GitHub, the needs-human owner's gate busy) makes the item's whole
episode UNKNOWN: its triage is owed and never covered, so the rule fails
toward triaging again, never toward silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ..domain.blocked_item_triage import block_episode

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports.issue import Issue
    from .label_manager import LabelManager
    from .needs_human_episodes import NeedsHumanEpisodes

logger = logging.getLogger(__name__)

#: ``{casefolded label: "<applied_at>#<event id>"}`` for one item's labels.
_Onsets = dict[str, str]


class BlockEpisodes:
    """THE reader of blocked items' block episodes for the triage rule (#8731)."""

    def __init__(
        self,
        *,
        needs_human: "NeedsHumanEpisodes",
        label_applications: Callable[[int, Sequence[str]], Mapping[str, "LabelEvent | None"]],
        labels: "LabelManager",
        clock: Callable[[], float],
        recheck_seconds: Callable[[], float],
    ) -> None:
        self._needs_human = needs_human
        self._label_applications = label_applications
        self._labels = labels
        self._clock = clock
        self._recheck_seconds = recheck_seconds
        self._checked_at: float | None = None
        #: The label onsets the last verification proved, per item. An item
        #: (or a label) missing here is unknown until a verification proves it.
        self._onsets: dict[int, _Onsets] = {}

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """Each blocked item's episode, every part read from GitHub now.

        An item any part of whose episode cannot be verified is left out: its
        episode is unknown.
        """
        needs_human = self._needs_human.verified(self._holding_needs_human(issues))
        self._onsets = {}
        for number, issue in issues.items():
            onsets = self._read_onsets(number, self._dated_labels(issue))
            if onsets is not None:
                self._onsets[number] = onsets
        return self._compose(issues, needs_human)

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """The episodes, re-verified once per recheck period.

        The tick reads this, so it makes no GitHub read between rechecks. A
        recheck that finds a new application is a new episode, and the triage
        made on the old one no longer covers the item. An item the last
        recheck could not verify stays unknown (owed) until one does.
        """
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._recheck_seconds():
            self._checked_at = now
            return self.verified(issues)
        return self._compose(issues, self._needs_human.recorded(self._holding_needs_human(issues)))

    def _compose(self, issues: Mapping[int, "Issue"], needs_human: Mapping[int, str]) -> dict[int, str]:
        episodes: dict[int, str] = {}
        for number, issue in issues.items():
            held = self._holds_needs_human(issue)
            if held and number not in needs_human:
                continue  # the needs-human part is unknown
            proven = self._onsets.get(number, {})
            dated = self._dated_labels(issue)
            if any(label not in proven for label in dated):
                continue  # a label not verified since it was put on is unknown
            episodes[number] = block_episode(
                needs_human[number] if held else None, {label: proven[label] for label in dated},
            )
        return episodes

    def _read_onsets(self, number: int, labels: Sequence[str]) -> _Onsets | None:
        if not labels:
            return {}
        try:
            applications = self._label_applications(number, labels)
        except Exception:
            logger.warning(
                "[TRIAGE] events of #%d unreadable; its block episode is unverified",
                number, exc_info=True,
            )
            return None
        onsets: _Onsets = {}
        for label in labels:
            application = applications[label]
            if application is None:
                return None  # GitHub does not show it standing: the cache is stale
            onsets[label] = f"{application.created_at}#{application.event_id}"
        return onsets

    def _dated_labels(self, issue: "Issue") -> tuple[str, ...]:
        """The casefolded blocking labels this owner dates: every one but the
        shared needs-human label, whose episode its own owner records."""
        needs_human = self._labels.needs_human.casefold()
        return tuple(sorted(
            {label.casefold() for label in self._labels.get_blocking(issue.labels)} - {needs_human}
        ))

    def _holds_needs_human(self, issue: "Issue") -> bool:
        """The block is (in part) the shared needs-human block: its label or
        the tech-lead hand-over marker is on the item."""
        folded = {label.casefold() for label in issue.labels}
        return bool(
            folded & {self._labels.needs_human.casefold(), self._labels.tech_lead_needs_human.casefold()}
        )

    def _holding_needs_human(self, issues: Mapping[int, "Issue"]) -> dict[int, "Issue"]:
        return {number: issue for number, issue in issues.items() if self._holds_needs_human(issue)}


class _UnwiredEpisodes(BlockEpisodes):
    """Stands in until the composition binds the real owner: reading raises."""

    def __init__(self) -> None:
        pass

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        raise RuntimeError("block episodes are not wired (#8688, #8731)")

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        raise RuntimeError("block episodes are not wired (#8688, #8731)")


UNWIRED_EPISODES: BlockEpisodes = _UnwiredEpisodes()
