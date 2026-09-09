"""The recovery target artifact and launch grant come from one captured read."""

import json

from issue_orchestrator.control.tech_lead_recovery_targets import (
    VALIDATED_WORK_RECOVERY_TARGETS_FILENAME,
    prepare_validated_work_recovery_targets,
)
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkKey,
)
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)


def _snapshot(issue_number: int) -> ValidatedWorkAuthoritySnapshot:
    key = ValidatedWorkKey("owner/repo", issue_number, f"issue-{issue_number}", "a" * 40)
    return ValidatedWorkAuthoritySnapshot(
        record_id=key.record_id,
        evidence_id=f"evidence-{issue_number}",
        observation_revision=3,
        validated_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        repo_slug=key.repo_slug,
        issue_number=issue_number,
        pr_number=None,
        expected_remote_head_sha=None,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
    )


def test_writes_exactly_the_snapshots_returned_by_the_authority(tmp_path):
    snapshots = (_snapshot(41), _snapshot(42))

    class Authority:
        def grants_for(self, issue_numbers):
            assert issue_numbers == (42, 41)
            return snapshots

    result = prepare_validated_work_recovery_targets(
        data_dir=tmp_path,
        authority=Authority(),
        issue_numbers=(42, 41),
    )

    assert result == snapshots
    assert json.loads(
        (tmp_path / VALIDATED_WORK_RECOVERY_TARGETS_FILENAME).read_text()
    ) == [snapshot.to_dict() for snapshot in snapshots]
