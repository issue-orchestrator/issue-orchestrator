"""Transport-safe observations preserve each exact durable work disposition."""

from .validated_work_commands import ValidatedWorkDispositionBatch


def disposition_observation(batch: ValidatedWorkDispositionBatch) -> dict[str, object]:
    return {"issue_number": batch.issue_number, "reason": batch.reason,
        "dispositions": [{"record_id": row.record_id, "evidence_id": row.evidence_id,
            "state": row.state.value, "failure": row.failure.value if row.failure else None,
            "branch_name": row.key.branch_name, "validated_head_sha": row.key.validated_head_sha,
            "repo_slug": row.key.repo_slug} for row in batch.dispositions]}
