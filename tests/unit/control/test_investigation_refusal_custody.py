"""A refused investigation's checkout is held by git, not by a promise (#7263).

The escalation tells an operator the branch is preserved. What makes that TRUE
is git's own lock: startup reconciliation classifies an inactive scratch
checkout as disposable and removes it WITH its never-pushed branch, and a locked
checkout it retains. So the lock is the custody record, and the settlement that
drops the queued retry is only safe because the lock was taken first.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.session_launch_types import (
    LaunchDisposition,
    LaunchResult,
)
from issue_orchestrator.control.worktree_reconciliation import _disposable_entry
from issue_orchestrator.domain.models import PendingValidationRetry
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_scratch_identity import (
    new_scratch_identity,
)
from issue_orchestrator.ports.worktree_manager import RegisteredWorktree


class _Recorder:
    def __init__(self, *, lock_succeeds: bool = True) -> None:
        self.lock_succeeds = lock_succeeds
        self.locked: list[tuple[Path, str]] = []
        self.escalations: list[dict] = []

    def lock_checkout(self, worktree_path: Path, *, reason: str) -> bool:
        if not self.lock_succeeds:
            return False
        self.locked.append((worktree_path, reason))
        return True

    def escalate(self, **kwargs: object) -> bool:
        self.escalations.append(kwargs)
        return True


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


class TestReconciliationHonoursTheLock:
    """The half that makes the custody real, asserted rather than assumed."""

    def _entry(self, *, locked: bool):
        identity = new_scratch_identity("issue-orchestrator", 6410)
        path = Path("/w") / identity.worktree_name
        return _disposable_entry(
            path,
            RegisteredWorktree(
                path=path,
                head="abc1234",
                branch=identity.branch_name,
                locked=locked,
            ),
            kind="tech_lead_scratch",
            candidate_reason="owned disposable scratch worktree is inactive",
            activity=_Activity(),
        )

    def test_an_unlocked_inactive_scratch_checkout_is_a_cleanup_candidate(
        self,
    ) -> None:
        """Which is exactly what would destroy the branch."""
        assert self._entry(locked=False).disposition == "cleanup_candidate"

    def test_a_locked_one_is_retained(self) -> None:
        entry = self._entry(locked=True)

        assert entry.disposition == "retained"
        assert "locked" in entry.reason


class _Activity:
    """No active sessions: the state a restart starts from."""

    active_paths: frozenset[Path] = frozenset()


class TestQuarantineTakesCustodyFirst:
    def test_the_lock_is_taken_before_the_record_is_dropped(self) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder()

        result = quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            lock_checkout=recorder.lock_checkout,
            escalate=recorder.escalate,
        )

        assert isinstance(result, LaunchResult)
        assert result.disposition is LaunchDisposition.QUARANTINED
        assert recorder.locked, "the record was dropped without taking custody"
        assert "Unlock when resolved" in recorder.locked[0][1]

    def test_a_checkout_that_cannot_be_locked_keeps_its_record(self) -> None:
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder(lock_succeeds=False)

        result = quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            lock_checkout=recorder.lock_checkout,
            escalate=recorder.escalate,
        )

        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        assert recorder.escalations == [], (
            "it told a human the branch was safe when nothing was holding it"
        )

    def test_a_failed_escalation_does_not_undo_the_custody(self) -> None:
        """The comment is best-effort; the lock is the guarantee."""
        from issue_orchestrator.control.tech_lead_session_policy import (
            quarantine_retry_launch,
        )

        identity = new_scratch_identity("issue-orchestrator", 6410)
        recorder = _Recorder()

        result = quarantine_retry_launch(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name),
            "because",
            lock_checkout=recorder.lock_checkout,
            escalate=lambda **_: False,
        )

        assert result.disposition is LaunchDisposition.QUARANTINED
        assert recorder.locked


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
