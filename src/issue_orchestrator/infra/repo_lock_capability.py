"""Opaque proof issued by the repository lock owner after startup succeeds."""

from collections.abc import Callable
from typing import Never

from ..domain.validated_work_claim import ProcessIdentity

# Private issuer is shared only with its sibling repository lock owner.
__all__ = ["HeldStartupGate", "gate_identity", "gate_proves_death", "_issue_gate"]


class HeldStartupGate:
    """Revocable process-bound capability; metadata cannot manufacture one."""

    __slots__ = ()

    def __new__(cls) -> Never:
        raise TypeError("startup gates are issued by repo_lock.held_startup_gate")

    def __reduce__(self) -> Never:
        raise TypeError("startup gate capabilities cannot be copied or serialized")


# Only the repo-lock owner supplies these operations after acquiring its gates.
# Kept private: consumers receive the opaque identity, never closures or fds.
_OPERATIONS: dict[
    HeldStartupGate,
    tuple[Callable[[], ProcessIdentity], Callable[[ProcessIdentity], bool]],
] = {}


def _issue_gate(
    current: Callable[[], ProcessIdentity],
    dead: Callable[[ProcessIdentity], bool],
) -> HeldStartupGate:
    capability = object.__new__(HeldStartupGate)
    _OPERATIONS[capability] = (current, dead)
    return capability


def gate_identity(capability: HeldStartupGate) -> ProcessIdentity:
    """Read the owning process identity, requiring a still-held startup gate."""
    operations = _OPERATIONS.get(capability)
    if operations is None:
        raise RuntimeError("unknown startup gate capability")
    return operations[0]()


def gate_proves_death(capability: HeldStartupGate, owner: ProcessIdentity) -> bool:
    """Return only positive kernel-gate evidence from the capability issuer."""
    operations = _OPERATIONS.get(capability)
    return operations is not None and operations[1](owner)
