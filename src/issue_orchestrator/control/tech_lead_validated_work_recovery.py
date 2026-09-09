"""Tech-lead adapter over the shared validated-work recovery owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import (
    DispositionInitiator,
    StoredEvidenceCommand,
)
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .actions import ActionResult, RecoverValidatedWorkAction
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced

logger = logging.getLogger(__name__)
_RATIONALE_PREVIEW_CHARS = 500
RecoveryResult = RecoveryCompleted | RecoveryAttemptPending


@dataclass
class TechLeadValidatedWorkRecoveryExecutor:
    """Translate tech-lead authority into one shared-owner recovery command."""

    events: EventSink
    preflight: Callable[[StoredEvidenceCommand], RecoveryAttemptPending | None]
    recover: Callable[[StoredEvidenceCommand], RecoveryResult]

    @staticmethod
    def _command(action: RecoverValidatedWorkAction) -> StoredEvidenceCommand:
        return StoredEvidenceCommand(
            issue_number=action.issue_number,
            reason=action.rationale,
            initiator=DispositionInitiator.TECH_LEAD,
            evidence_id=action.authority.evidence_id,
            actor=(
                f"proposal:{action.proposal_issue_number}"
                if action.proposal_issue_number
                else f"tech-lead:{action.proposal_id}"
            ),
            authority=action.authority,
        )

    def proposal_stale_reason(self, action: RecoverValidatedWorkAction) -> str | None:
        """Check reuse through the shared current-record policy without writes."""
        pending = self.preflight(self._command(action))
        return pending.message if pending is not None else None

    def apply(self, action: RecoverValidatedWorkAction) -> ActionResult:
        command = self._command(action)
        pending = self.preflight(command)
        result = pending if pending is not None else self.recover(command)
        if isinstance(result, RecoveryAttemptPending):
            if result.failure is ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE:
                return self._downgrade(action, result)
            return ActionResult.fail(
                action,
                result.message,
                issue_number=action.issue_number,
                proposal_id=action.proposal_id,
            )
        target = result.target
        boundary = {
            "record_id": action.authority.record_id,
            "evidence_id": action.authority.evidence_id,
            "validated_head_sha": target.key.validated_head_sha,
            "pr_number": target.pr_number,
            "pr_url": target.pr_url,
        }
        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_ACTION_EXECUTED,
                {
                    "issue_number": action.anchor_issue_number,
                    "action_id": action.proposal_id,
                    "proposal_type": "recover_validated_work",
                    "target_number": action.issue_number,
                    "finding_ids": list(action.finding_ids),
                    "boundary": boundary,
                },
            )
        )
        logger.info(
            issue_log(
                action.issue_number,
                "Tech Lead recover_validated_work %s executed via the recovery owner",
            ),
            action.proposal_id,
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
            terminal_disposition_satisfied=True,
            pr_number=target.pr_number,
        )

    def _downgrade(
        self, action: RecoverValidatedWorkAction, pending: RecoveryAttemptPending
    ) -> ActionResult:
        stale = pending.authority_stale
        if stale is None:
            raise ValueError("explicit recovery stale refusal omitted authority facts")
        logger.warning(
            issue_log(
                action.issue_number,
                "Tech Lead recover_validated_work %s downgraded: %s",
            ),
            action.proposal_id,
            pending.message,
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type="recover_validated_work",
            target_number=action.issue_number,
            target_is_pr=False,
            title="",
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=pending.message,
            boundary=stale.to_dict(),
        )
        return ActionResult.skip(
            action,
            f"stale precondition: {pending.message}",
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
            authority_stale_fields=list(stale.differences()),
        )
