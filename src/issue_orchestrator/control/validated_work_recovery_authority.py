"""Own selection of immutable authority for explicit validated-work recovery.

Automatic recovery needs no approval snapshot. A human-approved tech-lead or
operator recovery does: the snapshot must identify one current, retained,
approval-required head. This owner performs that selection once at the launch
boundary. Later planning can only echo the resulting immutable grant; it never
re-reads mutable store state and silently retargets an agent decision.
"""

from collections.abc import Sequence

from ..domain.validated_work import (
    EvidenceRole,
    LineageRole,
    RemoteBaselineStatus,
    ValidatedWorkState,
)
from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore


class ValidatedWorkRecoveryAuthority:
    """Project unambiguous PARKED head records into launch-time grants."""

    def __init__(self, store: ValidatedWorkAdmissionStore) -> None:
        self._store = store

    def grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        grants: list[ValidatedWorkAuthoritySnapshot] = []
        for issue_number in sorted(set(issue_numbers)):
            candidates = self._candidates(issue_number)
            # The agent command names an issue, so zero or multiple eligible
            # records cannot be represented safely. Keep either case out of
            # launch authority instead of choosing an arbitrary branch.
            if len(candidates) == 1:
                grants.append(candidates[0])
        return tuple(grants)

    def _candidates(
        self, issue_number: int
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        dispositions = {
            item.record_id: item
            for item in self._store.for_issue(issue_number).dispositions
        }
        return tuple(
            row.authority
            for row in self._store.retained_evidence(issue_number)
            if row.role is EvidenceRole.CURRENT
            and not row.released_at
            and row.record_id in dispositions
            and dispositions[row.record_id].evidence_id == row.evidence_id
            and dispositions[row.record_id].state is ValidatedWorkState.PARKED
            and dispositions[row.record_id].lineage_role is LineageRole.HEAD
            and row.authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED
        )
