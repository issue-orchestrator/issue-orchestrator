"""Apply and observe one desired label state under the supplied effect scope."""

from typing import Protocol
from ..domain.recovery_block import require_cleanup_absence

from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.synchronous_effects import SynchronousEffectScope
from .actions import ActionResult, AddLabelAction, RemoveLabelAction


class RecoveryLabelApplier(Protocol):
    def apply(self, action: AddLabelAction | RemoveLabelAction) -> ActionResult: ...


class RecoveryLabels:
    def __init__(self, reader: FreshIssueReader, applier: RecoveryLabelApplier) -> None:
        self._reader = reader
        self._applier = applier

    def require_absent(
        self, issue: int, label: str, effects: SynchronousEffectScope
    ) -> None:
        """Observe ambiguous cleanup without repeating a possible committed write."""
        current = effects.perform(lambda: self._reader.read_issue_labels(issue))
        require_cleanup_absence(label, tuple(current))

    def set_presence(
        self,
        issue: int,
        label: str,
        effects: SynchronousEffectScope,
        *,
        present: bool,
    ) -> tuple[str, ...]:
        """Return names actually changed; an unobserved write raises for retry."""
        current = effects.perform(lambda: self._reader.read_issue_labels(issue))
        if (label in current) == present:
            return ()
        action = (AddLabelAction if present else RemoveLabelAction)(
            issue_number=issue,
            label=label,
            reason="aggregate validated-work disposition",
        )
        result = effects.perform(lambda: self._applier.apply(action))
        if not result.success:
            raise RuntimeError(result.error or "recovery label write refused")
        observed = effects.perform(lambda: self._reader.read_issue_labels(issue))
        if (label in observed) != present:
            raise RuntimeError("recovery label write not observed")
        return (label,)
