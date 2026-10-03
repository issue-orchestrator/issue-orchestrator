"""The shared block owner's resolution command (#7658), kept beside it.

:class:`~.needs_human_block.NeedsHumanBlock` is the one owner of the shared
``needs-human`` label and its causes; this mixin is its ``resolve`` command,
split into its own module only because the owner's file is at its size
budget. It uses the owner's own gate and internals, so it is the same owner,
not a second one.

A resolution discharges, in a human's stead, exactly the work-block causes a
tech lead decided (:func:`~..domain.block_resolution.is_resolvable_work_block`),
all or nothing: every named cause must stand on the LIVE label, and the label
comes off only when no other cause holds it. Otherwise nothing is touched,
and the outcome says whether any write was attempted (only the owner knows),
which is what the caller's write-ahead record of the discharge needs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from ..domain.block_resolution import is_resolvable_work_block
from ..domain.human_block import BlockOutcome, HumanBlockRequest, NeedsHumanCause, ResolutionOutcome

T = TypeVar("T")

logger = logging.getLogger(__name__)

_NOTHING = ResolutionOutcome(BlockOutcome.FAILED, mutation_attempted=False)


class BlockResolutionCommand:
    """``resolve`` for the shared block's owner (see the module docstring)."""

    __slots__ = ()

    if TYPE_CHECKING:  # the owner's own gate and internals
        def _mutate(self, target: int, operation: Callable[[], T], *, busy: T) -> T: ...
        def _label_present_now(self, issue_number: int) -> bool | None: ...
        def _recorded_cause_holds(self, cause: NeedsHumanCause, issue_number: int) -> bool: ...
        def _holds(self, cause: NeedsHumanCause, issue_number: int) -> bool: ...
        def _withdraw(self, request: HumanBlockRequest) -> None: ...
        def _take_label_off(self, target: int, reason: str) -> BlockOutcome: ...

    def resolve(self, target: int, causes: frozenset[NeedsHumanCause], reason: str) -> ResolutionOutcome:
        outside = sorted(cause.value for cause in causes if not is_resolvable_work_block(cause))
        if not causes or outside:  # the typed rule, held by the owner itself
            raise ValueError(f"only work-block causes are resolvable, not {outside or 'none'}")
        return self._mutate(target, lambda: self._resolve(target, causes, reason), busy=_NOTHING)

    def _resolve(self, target: int, causes: frozenset[NeedsHumanCause], reason: str) -> ResolutionOutcome:
        if not self._label_present_now(target):  # unreadable, or already cleared: not ours
            return _NOTHING
        if not all(self._recorded_cause_holds(cause, target) for cause in causes):
            return _NOTHING  # all of the decision, or none of it
        if any(self._holds(cause, target) for cause in NeedsHumanCause if cause not in causes):
            for cause in causes:
                self._withdraw(HumanBlockRequest(target=target, cause=cause, reason=reason))
            logger.info("[BLOCK] #%d keeps needs-human after a resolution: another cause holds it", target)
            return ResolutionOutcome(BlockOutcome.HELD_BY_ANOTHER_CAUSE, mutation_attempted=True)
        return ResolutionOutcome(self._take_label_off(target, reason), mutation_attempted=True)
