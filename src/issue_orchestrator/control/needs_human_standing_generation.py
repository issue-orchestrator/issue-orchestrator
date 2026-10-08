"""The shared block owner's check against the generation GitHub shows (#8774).

:class:`~.needs_human_block.NeedsHumanBlock` sees only its own writes, so a
person who takes ``needs-human`` off and puts it back by hand between two of
its observations is invisible to it: the label reads present both times. The
person's clear ended every cause of the old generation, and their re-applied
label is a new block, theirs alone. Releasing an old cause must not take it
off.

GitHub sees every write. A generation is bound to the ``labeled`` event that
put its label on (:mod:`..execution.needs_human_generation`, #8688), and a
different standing event retires the old generation's causes. So the owner
binds:

* a generation it opens, right after its own label write;
* the standing generation, before a row-backed cause joins it;
* the standing generation, before the last row-backed cause takes the label off,
  which then goes only if that cause's row survived the binding.

Every row-backed release also needs its row on the live label. A cause the
owner does not have on record never put this label on, so it is not that
cause's to remove.

Kept beside the owner, as a mixin like its ``resolve`` command, because the
owner's file is at its size budget. It uses the owner's own gate and
internals, so it is the same owner, not a second one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING

from ..domain.human_block import BlockOutcome, HumanBlockRequest

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports.pending_work_claim_store import NeedsHumanCauseStore

logger = logging.getLogger(__name__)


class GenerationJoin(Enum):
    """Which generation of the shared label an acquisition's record joined."""

    #: The label was absent: this acquisition opened a new generation.
    OPENED = "opened"
    #: The label was present: the acquisition joined the generation standing.
    JOINED = "joined"
    #: The record was not written: the label, its events or the store were
    #: unreadable, so which generation it would join is unknown.
    UNKNOWN = "unknown"


class StandingGenerationGuard:
    """The owner's GitHub-bound generation checks (see the module docstring)."""

    __slots__ = ()

    if TYPE_CHECKING:  # the owner's own fields and internals
        needs_human_label: str
        label_application: Callable[[int, str], "LabelEvent | None"]
        causes: NeedsHumanCauseStore

        def _label_present_now(self, issue_number: int) -> bool | None: ...
        def _forget(self, target: int) -> None: ...

    def _bind_standing_generation(self, target: int) -> bool:
        """Bind the block's generation to the label application GitHub shows.

        A different standing application retires the old generation's causes
        and removal intent. False when that application is unknown: the
        events are unreadable, or GitHub does not show the label standing.
        """
        try:
            application = self.label_application(target, self.needs_human_label)
        except Exception:
            logger.exception(
                "[BLOCK] Could not read #%d's %s events, so which generation of "
                "the shared block stands is unknown", target, self.needs_human_label,
            )
            return False
        if application is None:
            logger.warning(
                "[BLOCK] GitHub does not show %s standing on #%d, though its "
                "labels do; which generation stands is unknown", self.needs_human_label, target,
            )
            return False
        try:
            self.causes.bind_needs_human_episode(
                target, event_id=application.event_id, applied_at=application.created_at,
            )
        except Exception:
            logger.exception("[BLOCK] Could not bind #%d's needs-human generation", target)
            return False
        return True

    def _bind_applied_generation(self, target: int) -> None:
        """Bind a generation this owner just opened to its own label write.

        Unbound, it would be bound by its next verification to whatever
        application stands then, which may already be a person's
        re-application, so a release before that verification could no longer
        tell the two apart. The label is on and its cause recorded whether or
        not this read succeeds; a failed one leaves the generation unbound.
        """
        if not self._bind_standing_generation(target):
            logger.warning(
                "[BLOCK] #%d's new needs-human generation is unbound until its "
                "next verification", target,
            )

    def _unrecorded_release_refusal(
        self, request: HumanBlockRequest
    ) -> BlockOutcome | None:
        """A row-backed release with no row on the live label removes nothing.

        Nothing of this cause stands on the label, so it is not this cause's
        to take off: a person's block, or another lifecycle's. An absent label
        retires any stale rows and reports the block cleared.
        """
        present = self._label_present_now(request.target)
        if present is None:
            return BlockOutcome.FAILED
        if not present:
            self._forget(request.target)
            return BlockOutcome.CLEARED
        try:
            recorded = self.causes.needs_human_causes(request.target)
        except Exception:
            logger.exception("[BLOCK] Cannot verify the release of #%d", request.target)
            return BlockOutcome.FAILED
        if request.cause_key not in recorded:
            return BlockOutcome.HELD_BY_ANOTHER_CAUSE
        return None

    def _stale_generation_refusal(
        self, request: HumanBlockRequest
    ) -> BlockOutcome | None:
        """The last row-backed cause takes the label off only from its own generation.

        Binds the standing generation first. If the label was put back by hand
        since this cause was recorded, the binding retired the cause, and the
        label stays: it is the person's block now, not this cause's.
        """
        if not self._bind_standing_generation(request.target):
            return BlockOutcome.FAILED
        try:
            recorded = self.causes.needs_human_causes(request.target)
        except Exception:
            logger.exception("[BLOCK] Cannot verify the release of #%d", request.target)
            return BlockOutcome.FAILED
        if request.cause_key in recorded:
            return None
        logger.warning(
            "[BLOCK] #%d's needs-human was taken off and put back by hand since "
            "%s was recorded; that ended it, and the new block stays",
            request.target, request.cause.value,
        )
        return BlockOutcome.HELD_BY_ANOTHER_CAUSE


__all__ = ["GenerationJoin", "StandingGenerationGuard"]
