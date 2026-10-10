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
  of the issue's events dates all of an item's labels, the needs-human one
  included (under that block owner's gate).

This owner reads them on one schedule:

* a health review's agenda verifies every item (:meth:`BlockEpisodes.verified`)
  before granting any, so every triage is made on the episode standing then;
* the tick's "is a triage owed" check (:meth:`BlockEpisodes.current`) re-reads
  every item at most once per health-review interval. Between rechecks it
  reads the onsets it proved, forgets one as soon as the tick's snapshot shows
  its label (or its item's block) gone, and reads GitHub at once for an item
  carrying a label it has no proven onset for (a new block, or one re-raised
  since it was seen lifted), so a re-raised block makes a review due on the
  tick that sees it, and a re-raise the snapshot never saw lifted by the next
  recheck, without an event scan of every item on every tick.

A part whose onset cannot be verified (events unreadable, the label not
standing on GitHub, the needs-human owner's gate busy) makes the item's whole
episode UNKNOWN: its triage is owed and never covered, so the rule fails
toward triaging again, never toward silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ..domain.blocked_item_triage import BlockEpisode, block_episode

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
        #: The label onsets proven per item. An item (or a label) missing here
        #: is unknown until a verification proves it.
        self._onsets: dict[int, _Onsets] = {}
        #: Items whose needs-human part was verified while the snapshot showed
        #: it held: one seen lifted and put back is read afresh (#8731 r2 F1).
        self._held: set[int] = set()
        #: Items whose last verification failed: unknown, and not re-read
        #: until the next recheck (no GitHub read on every tick for them).
        self._failed: set[int] = set()

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, BlockEpisode]:
        """Each blocked item's episode, every part read from GitHub now.

        An item any part of whose episode cannot be verified is left out: its
        episode is unknown.
        """
        self._onsets, self._held, self._failed = {}, set(), set()
        return self._compose(issues, self._verify(issues))

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, BlockEpisode]:
        """The episodes, every item re-verified once per recheck period.

        The tick reads this. Between rechecks it reads GitHub only for an item
        carrying a label with no proven onset. A recheck that finds a new
        application is a new episode, and the triage made on the old one no
        longer covers the item. An item a verification could not verify stays
        unknown (owed) until the next recheck verifies it.
        """
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._recheck_seconds():
            self._checked_at = now
            return self.verified(issues)
        self._forget_lifted(issues)
        unproven = {
            number: issue for number, issue in issues.items()
            if number not in self._failed and not self._proven(number, issue)
        }
        if unproven:
            self._verify(unproven)
        return self._compose(issues, self._needs_human.recorded(self._holding_needs_human(issues)))

    def _verify(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """Read *issues* from GitHub (one events scan each), record each one's
        proven label onsets, and return the needs-human episodes verified."""
        held = self._holding_needs_human(issues)
        needs_human, read = self._needs_human.verified(
            held, {number: self._dated_labels(issue) for number, issue in held.items()},
        )
        for number, issue in issues.items():
            dated = self._dated_labels(issue)
            applications = read.get(number) if number in held else self._read(number, dated)
            onsets = None if applications is None else _proven(applications, dated)
            if onsets is None or (number in held and number not in needs_human):
                self._onsets.pop(number, None)
                self._held.discard(number)
                self._failed.add(number)
            else:
                self._onsets[number] = onsets
                if number in held:
                    self._held.add(number)
                self._failed.discard(number)
        return needs_human

    def _proven(self, number: int, issue: "Issue") -> bool:
        """Every part of the item's block was verified since the snapshot last
        showed it lifted: each dated label, and the needs-human part."""
        if self._holds_needs_human(issue) and number not in self._held:
            return False
        proven = self._onsets.get(number, {})
        return all(label in proven for label in self._dated_labels(issue))

    def _forget_lifted(self, issues: Mapping[int, "Issue"]) -> None:
        """Forget every part the snapshot no longer shows on its blocked item
        (a label, the needs-human part, the whole block), so a re-raise seen
        later is read afresh (#8731 r1 F1, r2 F1)."""
        for number in set(self._onsets) - set(issues):
            del self._onsets[number]
        self._failed &= set(issues)
        self._held &= set(self._holding_needs_human(issues))
        for number, onsets in self._onsets.items():
            dated = set(self._dated_labels(issues[number]))
            self._onsets[number] = {label: onset for label, onset in onsets.items() if label in dated}

    def _compose(
        self, issues: Mapping[int, "Issue"], needs_human: Mapping[int, str],
    ) -> dict[int, BlockEpisode]:
        episodes: dict[int, BlockEpisode] = {}
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

    def _read(self, number: int, labels: Sequence[str]) -> Mapping[str, "LabelEvent | None"] | None:
        if not labels:
            return {}
        try:
            return self._label_applications(number, labels)
        except Exception:
            logger.warning(
                "[TRIAGE] events of #%d unreadable; its block episode is unverified",
                number, exc_info=True,
            )
            return None

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


def _proven(applications: Mapping[str, "LabelEvent | None"], labels: Sequence[str]) -> _Onsets | None:
    """Each label's onset, or None when GitHub does not show one standing (the
    cached snapshot is stale): the item's episode is then unknown."""
    onsets: _Onsets = {}
    for label in labels:
        application = applications[label]
        if application is None:
            return None
        onsets[label] = f"{application.created_at}#{application.event_id}"
    return onsets


class _UnwiredEpisodes(BlockEpisodes):
    """Stands in until the composition binds the real owner: reading raises."""

    def __init__(self) -> None:
        pass

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, BlockEpisode]:
        raise RuntimeError("block episodes are not wired (#8688, #8731)")

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, BlockEpisode]:
        raise RuntimeError("block episodes are not wired (#8688, #8731)")


UNWIRED_EPISODES: BlockEpisodes = _UnwiredEpisodes()
