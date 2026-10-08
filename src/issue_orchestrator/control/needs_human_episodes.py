"""Which episode of the shared needs-human block each blocked item is in (#8688).

A blocked-item triage covers the block EPISODE it was decided on. The block's
one owner (:class:`~.needs_human_block.NeedsHumanBlock`) opens a generation when
it puts the label on afresh and ends it when the label comes off, but it sees
only its own writes. GitHub sees every write. This owner binds each generation
to GitHub's standing ``labeled`` event, read from the complete issue events
(:class:`~..ports.label_application.LabelApplicationReader`):

* a health review's agenda verifies every item before granting any, so every
  triage is made on an episode bound to the label application standing then;
* the tick's "is a triage owed" check reads the recorded episodes, and
  re-verifies them at most once per health-review interval, so a label taken
  off and put back by hand, unseen by the owner, still makes a review due
  without a GitHub event scan on every tick;
* an item verified before whose GitHub ``updated_at`` and recorded episode
  are both unchanged is not read again: a label write bumps ``updated_at``.
  That is proof only once the verifying read came in a later second than
  ``updated_at`` (GitHub keeps whole seconds, so a write in that same second
  would not change it), so a verification is remembered only then.

An item whose events cannot be read, that GitHub does not show the label
standing on, or whose block is being changed by its owner right now (the
owner's per-issue gate is busy) has an UNKNOWN episode. Its triage is owed and
never covered: the rule fails toward triaging again, never toward silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.issue_disposition_gate import IssueDispositionGateStatus

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports.issue import Issue
    from ..ports.pending_work_claim_store import NeedsHumanEpisodeReader
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

#: How long after an issue's ``updated_at`` a verifying read must come before
#: it is remembered: GitHub's whole-second timestamps, plus clock skew.
_SETTLED_SECONDS = 5.0


class NeedsHumanEpisodes:
    """THE reader of needs-human block episodes for the triage rule (#8688)."""

    def __init__(
        self,
        *,
        store: "NeedsHumanEpisodeReader",
        label_applications: Callable[[int, str], "LabelEvent | None"],
        labels: "LabelManager",
        clock: Callable[[], float],
        recheck_seconds: Callable[[], float],
    ) -> None:
        self._store = store
        self._label_applications = label_applications
        self._labels = labels
        self._clock = clock
        self._recheck_seconds = recheck_seconds
        self._checked_at: float | None = None
        #: ``issue -> (updated_at, episode)`` as last verified by this reader.
        self._verified: dict[int, tuple[str, str]] = {}
        #: Items the last verification could not verify: unknown until one does.
        self._unverified: set[int] = set()

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """Each item's episode, bound to GitHub's standing label application.

        An item whose episode cannot be verified is left out: its episode is
        unknown.
        """
        verified: dict[int, str] = {}
        for number, issue in issues.items():
            with self._store.mutate_needs_human(number) as status:
                if status is IssueDispositionGateStatus.BUSY:
                    episode = None  # the owner is changing this block right now
                else:
                    episode = self._verify(number, issue)
            if episode is None:
                self._verified.pop(number, None)
                continue
            verified[number] = episode
            if self._settled(issue.updated_at):
                self._verified[number] = (str(issue.updated_at), episode)
        self._unverified = set(issues) - set(verified)
        return verified

    def _verify(self, number: int, issue: "Issue") -> str | None:
        """Under the owner's gate: the remembered episode while nothing has
        been written to the issue or its generation since, else a fresh bind."""
        seen = self._verified.get(number)
        recorded = self._store.needs_human_episodes([number]).get(number)
        if seen is not None and seen == (issue.updated_at, recorded):
            return seen[1]
        return self._bind(number, self._episode_label(issue.labels))

    def _settled(self, updated_at: str | None) -> bool:
        """No write can still share ``updated_at``'s second (see the module doc)."""
        if updated_at is None:
            return False
        try:
            stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return self._clock() >= stamp + _SETTLED_SECONDS

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        """The recorded episodes, re-verified once per recheck period.

        The tick reads this, so it makes no GitHub read between rechecks. A
        recheck that finds a new application opens a new episode, and the
        triage made on the old one no longer covers the item. An item the last
        recheck could not verify stays unknown (owed) until one does.
        """
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= self._recheck_seconds():
            self._checked_at = now
            return self.verified(issues)
        recorded = self._store.needs_human_episodes(sorted(issues))
        return {number: episode for number, episode in recorded.items() if number not in self._unverified}

    def _bind(self, number: int, label: str) -> str | None:
        try:
            application = self._label_applications(number, label)
        except Exception:
            logger.warning(
                "[TRIAGE] %s events of #%d unreadable; its block episode is unverified",
                label, number, exc_info=True,
            )
            return None
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


class _UnwiredEpisodes(NeedsHumanEpisodes):
    """Stands in until the composition binds the real owner: reading raises."""

    def __init__(self) -> None:
        pass

    def verified(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        raise RuntimeError("needs-human episodes are not wired (#8688)")

    def current(self, issues: Mapping[int, "Issue"]) -> dict[int, str]:
        raise RuntimeError("needs-human episodes are not wired (#8688)")


UNWIRED_EPISODES: NeedsHumanEpisodes = _UnwiredEpisodes()
