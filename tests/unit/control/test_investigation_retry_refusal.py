"""What a refused investigation retry actually does (#7263).

The refusal hands the work to a human and lets the queue drop it. It does NOT
claim to protect the checkout: a disposable investigation worktree is inactive
from the moment its session ends, whether or not a retry was queued, so it is
subject to the same cleanup it always was. Giving it real custody is #7274 --
forced removal falls back to ``shutil.rmtree``, so no git-level hold binds it.
"""

from __future__ import annotations

from issue_orchestrator.control.session_launch_types import (
    LaunchDisposition,
    LaunchResult,
)
from issue_orchestrator.domain.models import PendingValidationRetry
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_scratch_identity import (
    new_scratch_identity,
)


class _Recorder:
    def __init__(self, *, notified: bool = True) -> None:
        self.notified = notified
        self.escalations: list[dict] = []

    def escalate(self, **kwargs: object) -> bool:
        self.escalations.append(kwargs)
        return self.notified


def _retry(worktree: str, branch: str) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=6410,
        issue_title="Investigate",
        agent_label="agent:backend",
        worktree_path=worktree,
        branch_name=branch,
        original_prompt=None,
        validation_error="boom",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
    )


class TestRefusalHandsOffBeforeDropping:
    def test_a_handed_off_record_leaves_the_queue(self) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder()

        result = quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            escalate=recorder.escalate,
        )

        assert isinstance(result, LaunchResult)
        assert result.disposition is LaunchDisposition.QUARANTINED
        assert len(recorder.escalations) == 1

    def test_a_handoff_that_fails_keeps_the_record(self) -> None:
        """The notification is the only thing protecting this work.

        Dropping the last queued reference to a never-pushed branch that nobody
        has been told about is the one outcome worse than a poison item.
        """
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder(notified=False)

        result = quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            escalate=recorder.escalate,
        )

        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        assert "handoff failed" in (result.reason or "")

    def test_the_handoff_names_the_branch_and_does_not_promise_it_is_held(
        self,
    ) -> None:
        """An operator acting on this must know it is not protected."""
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder()

        quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            escalate=recorder.escalate,
        )

        comment = recorder.escalations[0]["comment"]
        assert identity.branch_name in comment
        assert "never pushed" in comment
        assert "Nothing here holds the checkout open" in comment
        assert "salvage anything you need" in comment


class TestGuardLabelOwner:
    """The mode->guard-label choice, and its refusal of anything else.

    Both former copies treated every non-"coding" value as review, so a typo or
    a third mode silently cleared the wrong guard -- and a guard cleared on the
    wrong issue is a relaunch loop nothing stops.
    """

    def _config(self):
        from issue_orchestrator.infra.config_models import (
            InterruptedSessionRetryConfig,
        )

        return InterruptedSessionRetryConfig(
            coding_guard_label="io:coding-guard",
            review_guard_label="io:review-guard",
        )

    def test_each_supported_mode_maps_to_its_own_label(self) -> None:
        config = self._config()

        assert config.guard_label("coding") == "io:coding-guard"
        assert config.guard_label("review") == "io:review-guard"

    def test_an_unsupported_mode_is_refused_rather_than_treated_as_review(
        self,
    ) -> None:
        import pytest

        with pytest.raises(ValueError, match="no interrupted-retry guard label"):
            self._config().guard_label("rework")
