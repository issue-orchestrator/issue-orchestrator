"""Liveness from a required successful-startup-held repository gate."""

from ..domain.validated_work_claim import ProcessIdentity
from ..infra.repo_lock_capability import (
    HeldStartupGate,
    gate_identity,
    gate_proves_death,
)


class RepoLockLiveness:
    def __init__(self, gate: HeldStartupGate) -> None:
        gate_identity(gate)
        self._gate = gate

    def current(self) -> ProcessIdentity:
        return gate_identity(self._gate)

    def is_provably_dead(self, owner: ProcessIdentity) -> bool:
        return gate_proves_death(self._gate, owner)
