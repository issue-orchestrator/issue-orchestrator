"""Prepare the agent-visible and trusted halves of recovery launch authority."""

import json
from collections.abc import Sequence
from pathlib import Path

from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from ..ports.validated_work_recovery_authority import (
    ValidatedWorkRecoveryAuthorityReader,
)

VALIDATED_WORK_RECOVERY_TARGETS_FILENAME = "validated-work-recovery-targets.json"


def prepare_validated_work_recovery_targets(
    *,
    data_dir: Path,
    authority: ValidatedWorkRecoveryAuthorityReader,
    issue_numbers: Sequence[int],
) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
    """Capture once, expose the same snapshots to the agent, and return the grant."""
    snapshots = authority.grants_for(issue_numbers)
    (data_dir / VALIDATED_WORK_RECOVERY_TARGETS_FILENAME).write_text(
        json.dumps([snapshot.to_dict() for snapshot in snapshots], indent=2) + "\n",
        encoding="utf-8",
    )
    return snapshots
