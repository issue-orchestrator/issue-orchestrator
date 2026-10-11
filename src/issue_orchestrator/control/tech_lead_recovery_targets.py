"""Prepare the agent-visible and trusted halves of recovery launch authority."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
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


VALIDATED_WORK_RELEASE_TARGETS_FILENAME = "validated-work-release-targets.json"


def prepare_validated_work_release_targets(
    *,
    data_dir: Path,
    authority: ValidatedWorkRecoveryAuthorityReader,
    issue_numbers: Sequence[int],
) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
    """Records a ``release_validated_work`` may name, shown to the agent once (#9092).

    The returned tuple is the trusted grant recorded in launch authority; the
    agent names record ids from the file, and approval binds the grant's
    snapshot for each, never anything read later.
    """
    snapshots = authority.release_grants_for(issue_numbers)
    (data_dir / VALIDATED_WORK_RELEASE_TARGETS_FILENAME).write_text(
        json.dumps([snapshot.to_dict() for snapshot in snapshots], indent=2) + "\n",
        encoding="utf-8",
    )
    return snapshots


@dataclass(frozen=True)
class ValidatedWorkLaunchGrants:
    """Both validated-work grants one launch records, from one capture each."""

    recovery: tuple[ValidatedWorkAuthoritySnapshot, ...]
    release: tuple[ValidatedWorkAuthoritySnapshot, ...]


def prepare_validated_work_launch_grants(
    *,
    data_dir: Path,
    authority: ValidatedWorkRecoveryAuthorityReader,
    issue_numbers: Sequence[int],
) -> ValidatedWorkLaunchGrants:
    """Write both agent-visible target files and return the trusted grants."""
    return ValidatedWorkLaunchGrants(
        recovery=prepare_validated_work_recovery_targets(
            data_dir=data_dir, authority=authority, issue_numbers=issue_numbers
        ),
        release=prepare_validated_work_release_targets(
            data_dir=data_dir, authority=authority, issue_numbers=issue_numbers
        ),
    )
