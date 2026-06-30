"""Port: stack-predecessor fact gathering (ADR-0029).

The dependency gate report decides stack readiness from injected
:class:`~issue_orchestrator.domain.dependency_gates.PredecessorFacts` and does no
I/O itself. This port is the boundary that *gathers* those facts — branch
usability, validation, agent review, and merge state — for a stack
predecessor's branch head. The control layer (the dependency evaluator) depends
on this Protocol; the concrete git/PR implementation lives in ``execution/`` and
is wired at the composition root.

Implementations must be **fail-safe**: when a fact cannot be established for a
predecessor (no PR, cross-repo lookup unavailable, transient error), return
conservative :class:`PredecessorFacts` (all-False) so the dependent stack slice
stays blocked rather than launching on an unverified base.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from ..domain.dependencies import DependencyTarget
from ..domain.dependency_gates import PredecessorFacts


@runtime_checkable
class StackPredecessorFactsProvider(Protocol):
    """Gathers git/PR facts about stack predecessors' branch heads."""

    def gather_facts(
        self, targets: Sequence[DependencyTarget]
    ) -> Mapping[DependencyTarget, PredecessorFacts]:
        """Return predecessor facts keyed by repository-aware target.

        Only ``targets`` that are genuine stack predecessors are passed in. A
        target with no determinable facts may be omitted or mapped to a
        conservative :class:`PredecessorFacts`; the gate report treats a missing
        entry as "no usable branch yet" and keeps the dependent slice blocked.
        """
        ...
