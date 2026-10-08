"""Which episode of the shared needs-human block each blocked item is in (#8688).

A blocked-item triage covers the block EPISODE it was decided on. The block's
one owner (:class:`~.needs_human_block.NeedsHumanBlock`) opens a generation when
it puts the label on afresh and ends it when the label comes off, but it sees
only its own writes. GitHub sees every write. This owner binds each generation
to GitHub's standing ``labeled`` event, read from the complete issue events
(:class:`~..ports.label_application.LabelApplicationReader`):

* a health review's agenda verifies every item before granting any, so every
  triage is made on an episode bound to the label application standing then;
* the tick's "is a triage owed" check reads the recorded episodes between the
  rechecks :class:`~.block_episodes.BlockEpisodes` schedules (once per
  health-review interval), so a label taken off and put back by hand, unseen
  by the owner, still makes a review due without a GitHub event scan on every
  tick. A recheck reads every item: the cached issue snapshot may be hours
  old, so nothing local can prove an item unchanged (#8688 review r6).

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
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports.issue import Issue
    from ..ports.pending_work_claim_store import NeedsHumanEpisodeReader
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class NeedsHumanEpisodes:
    """THE reader of needs-human block episodes for the triage rule (#8688)."""

    def __init__(
        self,
        *,
        store: "NeedsHumanEpisodeReader",
        label_applications: Callable[[int, Sequence[str]], Mapping[str, "LabelEvent | None"]],
        labels: "LabelManager",
    ) -> None:
        self._store = store
        self._label_applications = label_applications
        self._labels = labels
        #: Items a verification could not verify: unknown until one does.
        self._unverified: set[int] = set()

    def verified(
        self, issues: Mapping[int, "Issue"], also: Mapping[int, Sequence[str]],
    ) -> tuple[dict[int, str], dict[int, Mapping[str, "LabelEvent | None"]]]:
        """``(episodes, applications)``: each item's episode, bound to GitHub's
        standing label application, and the standing application of each of
        the item's ``also`` labels, read in the SAME scan of its events
        (#8731), under the owner's gate.

        An item whose episode cannot be verified is left out of ``episodes``:
        its episode is unknown. An item whose events were not read (the gate
        busy, the read failed) is left out of ``applications`` too.
        """
        verified: dict[int, str] = {}
        applications: dict[int, Mapping[str, "LabelEvent | None"]] = {}
        for number, issue in issues.items():
            episode = None
            with self._store.mutate_needs_human(number) as status:
                # BUSY: the owner is changing this block right now.
                if status is not IssueDispositionGateStatus.BUSY:
                    label = self._episode_label(issue.labels).casefold()
                    read = self._read(number, (label, *also.get(number, ())))
                    if read is not None:
                        applications[number] = read
                        episode = self._bind(number, read[label])
            if episode is not None:
                verified[number] = episode
        self._unverified = (self._unverified - set(issues)) | (set(issues) - set(verified))
        return verified, applications

    def recorded(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """The recorded episodes, with no GitHub read: what the tick reads
        between rechecks. An item a verification could not verify stays
        unknown (owed) until one does; one no longer held is forgotten."""
        self._unverified &= set(issues)
        recorded = self._store.needs_human_episodes(sorted(issues))
        return {number: episode for number, episode in recorded.items() if number not in self._unverified}

    def _read(self, number: int, labels: Sequence[str]) -> Mapping[str, "LabelEvent | None"] | None:
        try:
            return self._label_applications(number, labels)
        except Exception:
            logger.warning(
                "[TRIAGE] events of #%d unreadable; its block episode is unverified",
                number, exc_info=True,
            )
            return None

    def _bind(self, number: int, application: "LabelEvent | None") -> str | None:
        if application is None:
            return None  # GitHub does not show it standing: the cache is stale
        return self._store.bind_needs_human_episode(
            number, event_id=application.event_id, applied_at=application.created_at,
        )

    def _episode_label(self, issue_labels: Sequence[str]) -> str:
        """The label whose application dates the episode: needs-human, else the marker."""
        needs_human = self._labels.needs_human
        if needs_human.casefold() in {label.casefold() for label in issue_labels}:
            return needs_human
        return self._labels.tech_lead_needs_human
