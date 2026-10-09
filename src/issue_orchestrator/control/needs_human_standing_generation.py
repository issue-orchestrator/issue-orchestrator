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

* a generation it opens, right after its own label write (the only binding
  that may adopt an unbound generation: any later one ends it);
* the standing generation, before a row-backed cause joins it;
* the standing generation, before the last cause of any kind takes the label
  off, which then goes only if the binding did not end the generation;
* the standing generation, before a resolution, a merge-hold move, or a
  merge-scoped hold lets work through (:meth:`standing_causes`,
  :meth:`hold_causes`).

Every row-backed release also needs its row on the live label. A cause the
owner does not have on record never put this label on, so it is not that
cause's to remove.

Kept beside the owner, as a mixin like its ``resolve`` command, because the
owner's file is at its size budget. It uses the owner's own gate and
internals, so it is the same owner, not a second one.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from enum import Enum
from typing import TYPE_CHECKING, TypeVar

from ..domain.human_block import (
    BlockOutcome,
    HumanBlockRequest,
    HumanHoldScope,
    NeedsHumanCause,
    hold_scope,
)
from ..ports.pending_work_claim_store import GenerationBinding

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports.pending_work_claim_store import NeedsHumanCauseStore

T = TypeVar("T")

#: How often a merge-scoped hold is re-checked against the standing generation
#: (r2 F3): a bound on GitHub event reads, not on correctness of anything else.
MERGE_SCOPE_RECHECK_SECONDS = 600.0

logger = logging.getLogger(__name__)


class StandingGenerationUnknown(RuntimeError):
    """Which generation of the shared block stands could not be read."""

    def __init__(self, issue_number: int) -> None:
        super().__init__(f"which needs-human generation stands on #{issue_number} is unknown")
        self.issue_number = issue_number


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
        merge_scope_checked: dict[int, float]
        own_write_verdict: Callable[["LabelEvent"], bool | None]

        def _label_present_now(self, issue_number: int) -> bool | None: ...
        def _forget(self, target: int) -> None: ...
        def _mutate(self, target: int, operation: Callable[[], T], *, busy: T) -> T: ...
        def recorded_causes(
            self, issue_numbers: Sequence[int]
        ) -> dict[int, frozenset[NeedsHumanCause]]: ...

    def standing_causes(self, issue_number: int) -> frozenset[NeedsHumanCause]:
        """The causes recorded on the generation GitHub shows standing now.

        For a decision that acts on a cause's ownership of the block (r1 F3):
        the raw record may still hold the causes of a generation a person
        ended by re-applying the label by hand. Binding first retires them.
        Raises :class:`StandingGenerationUnknown` when that cannot be read.
        """
        if not self._mutate(
            issue_number, lambda: self._bind_live_generation(issue_number), busy=False
        ):
            raise StandingGenerationUnknown(issue_number)
        return self.recorded_causes([issue_number])[issue_number]

    def hold_causes(self, issue_numbers: Sequence[int]) -> dict[int, frozenset[NeedsHumanCause]]:
        """The recorded causes, for deciding what a hold holds (#7678, r2 F3).

        Only a merge-scoped record lets work through a ``needs-human``, so it
        is the one a stale generation could loosen: a person who put the label
        back by hand meant a block, not the old merge question. It is checked
        against the standing generation at most once per
        :data:`MERGE_SCOPE_RECHECK_SECONDS`, and an unverifiable one holds the
        work. Every other record holds the work whatever its generation.
        """
        recorded = self.recorded_causes(issue_numbers)
        now = time.monotonic()
        for number, held in recorded.items():
            fresh = now - self.merge_scope_checked.get(number, -math.inf) < MERGE_SCOPE_RECHECK_SECONDS
            if not held or hold_scope(held) is not HumanHoldScope.MERGE or fresh:
                continue
            try:
                recorded[number] = self.standing_causes(number)
            except StandingGenerationUnknown:
                logger.warning("[BLOCK] #%d's merge hold is unverified; holding its work", number)
                recorded[number] = frozenset()
                continue
            self.merge_scope_checked[number] = now
        return recorded

    def _bind_live_generation(self, target: int) -> bool:
        """Bind the generation standing, or retire the rows of an absent label."""
        present = self._label_present_now(target)
        if present is None:
            return False
        if not present:
            self._forget(target)
            return True
        return self._bind_standing_generation(target) is not None

    def _bind_standing_generation(
        self, target: int, *, own_write: bool = False
    ) -> GenerationBinding | None:
        """Bind the block's generation to the label application GitHub shows.

        A different standing application (or an unbound generation, unless
        ``own_write``) ends the old generation and retires its causes and
        removal intent. None when that application is unknown: the events are
        unreadable, or GitHub does not show the label standing.
        """
        try:
            application = self.label_application(target, self.needs_human_label)
        except Exception:
            logger.exception(
                "[BLOCK] Could not read #%d's %s events, so which generation of "
                "the shared block stands is unknown", target, self.needs_human_label,
            )
            return None
        if application is None:
            logger.warning(
                "[BLOCK] GitHub does not show %s standing on #%d, though its "
                "labels do; which generation stands is unknown", self.needs_human_label, target,
            )
            return None
        if own_write and self._not_own_write(target, application):
            return None  # left unbound: its next binding ends it, failing closed
        try:
            return self.causes.bind_needs_human_generation(
                target, event_id=application.event_id, applied_at=application.created_at,
                own_write=own_write,
            )
        except Exception:
            logger.exception("[BLOCK] Could not bind #%d's needs-human generation", target)
            return None

    def _not_own_write(self, target: int, application: "LabelEvent") -> bool:
        """The standing application is provably someone else's (r5 F1).

        A person may take the label off and put it back between the owner's
        write and this read. An App engine knows its own writes by their
        actor, so it refuses to adopt one that is not; a personal-token
        engine writes as its user and cannot tell (None), so it adopts.
        """
        try:
            verdict = self.own_write_verdict(application)
        except Exception:
            logger.exception("[BLOCK] Could not tell whether #%d's label write is the owner's", target)
            return True
        if verdict is False:
            logger.warning(
                "[BLOCK] #%d's standing %s was applied by @%s, not this engine; "
                "leaving its new generation unbound", target, self.needs_human_label,
                application.actor_login,
            )
        return verdict is False

    def _bind_applied_generation(self, target: int) -> None:
        """Bind a generation this owner just opened to its own label write.

        Only this read may bind it: any later binding of a generation still
        unbound ends it (r2 F1), because a person's re-application in between
        would look the same. The label is on and its cause recorded whether
        or not this read succeeds; a failed one leaves the block to a person.
        """
        if self._bind_standing_generation(target, own_write=True) is None:
            logger.warning(
                "[BLOCK] #%d's new needs-human generation could not be bound to "
                "its own write; its causes end at its next binding", target,
            )

    def _cause_already_recorded(self, request: HumanBlockRequest) -> bool:
        """This cause's row already stands on the LIVE label generation.

        A row under an absent label is stale (a person cleared the label); the
        acquisition restarts the generation, so it is this call's row after
        all. Fail closed: an unreadable label or cause store counts as yes.
        """
        present = self._label_present_now(request.target)
        if present is None:
            return True
        if not present:
            return False
        try:
            return request.cause_key in self.causes.needs_human_causes(request.target)
        except Exception:
            logger.exception(
                "[BLOCK] Could not read needs-human causes for #%d", request.target
            )
            return True

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
        """The last cause takes the label off only from the generation it held.

        Binds the standing generation first. If the label was put back by hand
        since (the binding ENDED the generation, retiring its causes), the
        label stays: it is the person's block now. This holds for a
        self-recording cause too (r2 F2): its marker or ledger row says it
        still wants a block, not that the person's new one is its own.
        """
        binding = self._bind_standing_generation(request.target)
        if binding is None:
            return BlockOutcome.FAILED
        if binding is not GenerationBinding.ENDED:
            return None
        logger.warning(
            "[BLOCK] #%d's needs-human was taken off and put back by hand since "
            "%s held it; that ended it, and the new block stays",
            request.target, request.cause.value,
        )
        return BlockOutcome.HELD_BY_ANOTHER_CAUSE


__all__ = ["GenerationJoin", "StandingGenerationGuard", "StandingGenerationUnknown"]
