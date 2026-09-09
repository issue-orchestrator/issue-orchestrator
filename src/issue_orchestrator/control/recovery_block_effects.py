"""Effect scopes used only inside the aggregate owner's held issue gate."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..domain.published_work_finalization import RecoveryBlockReleaseRequest
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ClaimedRecoveryProjectionEffects:
    authority: ValidatedWorkEffectAuthority
    request: RecoveryBlockReleaseRequest

    def perform(self, effect: Callable[[], T]) -> T:
        return self.authority.perform(
            self.request.execution_token, self.request.claim, effect
        )


class IssueGateProjectionEffects:
    """Unclaimed reconciliation; never grants publication or record mutation."""

    def perform(self, effect: Callable[[], T]) -> T:
        return effect()
