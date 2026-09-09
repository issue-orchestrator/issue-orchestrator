"""Write-ahead cleanup of captured labels, preserving ambiguous later edits."""

from ..domain.recovery_block import RecoveryBlockPlan
from ..ports.recovery_block import RecoveryBlockStore
from ..ports.synchronous_effects import SynchronousEffectScope
from .recovery_labels import RecoveryLabels


class CapturedRecoveryCleanup:
    def __init__(self, records: RecoveryBlockStore, labels: RecoveryLabels) -> None:
        self._records = records
        self._labels = labels

    def apply(
        self,
        plan: RecoveryBlockPlan,
        permitted: frozenset[str],
        effects: SynchronousEffectScope,
    ) -> tuple[str, ...]:
        removed: list[str] = []
        for label in sorted(plan.captured_labels & permitted):
            first = effects.perform(
                lambda label=label: self._records.begin_block_label_cleanup(
                    plan.cleanup_keys, label
                )
            )
            if first:
                removed.extend(self._labels.set_presence(
                    plan.issue_number, label, effects, present=False
                ))
            else:
                self._labels.require_absent(plan.issue_number, label, effects)
        if plan.cleanup_keys and not effects.perform(
            lambda: self._records.acknowledge_block_cleanup(plan.cleanup_keys)
        ):
            raise RuntimeError("cleanup acknowledgement refused changed records")
        return tuple(removed)
