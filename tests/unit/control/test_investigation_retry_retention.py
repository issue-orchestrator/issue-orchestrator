"""A queued investigation retry's checkout is not disposable yet (#7263).

The retry's worktree is a tech-lead scratch checkout, and cleanup of a scratch
checkout removes its BRANCH with it (`action_applier` force-removes checkout and
branch for a disposable worktree). That branch holds the only copy of the
commits the retry exists to re-validate -- it is never pushed -- so a cleanup
that runs while the retry is still queued destroys the work and leaves the
queued request pointing at a branch that no longer exists.
"""

from __future__ import annotations

from issue_orchestrator.control.tech_lead_artifact_retention import (
    tech_lead_problem_artifact_hold_issue_numbers,
)
from issue_orchestrator.domain.models import (
    OrchestratorState,
    PendingValidationRetry,
)
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_scratch_identity import (
    new_scratch_identity,
)
from issue_orchestrator.infra.config import Config


def _retry(worktree_path: str, branch_name: str, issue: int = 6410):
    return PendingValidationRetry(
        issue_number=issue,
        issue_title="Investigate",
        agent_label="agent:backend",
        worktree_path=worktree_path,
        branch_name=branch_name,
        original_prompt=None,
        validation_error="boom",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
    )


def _config(*, tech_lead_enabled: bool = True) -> Config:
    config = Config(repo="test/repo")
    config.tech_lead_review_on_failure = tech_lead_enabled
    config.tech_lead_review_agent = "agent:tech-lead" if tech_lead_enabled else None
    return config


def _state(retry) -> OrchestratorState:
    state = OrchestratorState()
    state.pending_validation_retries.append(retry)
    return state


class TestInvestigationRetryRetention:
    def test_a_queued_investigation_retry_holds_its_checkout(self) -> None:
        identity = new_scratch_identity("issue-orchestrator", 6410)
        state = _state(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name)
        )

        held = tech_lead_problem_artifact_hold_issue_numbers(state, _config())

        assert 6410 in held, (
            "the retry's disposable checkout was released for cleanup, which"
            " deletes the branch holding the commits it must re-validate"
        )

    def test_the_hold_survives_tech_lead_review_being_turned_off(self) -> None:
        """Turning a feature off must not become a deletion.

        Every other entry in this hold set is about work that is about to
        happen, so the config gate is right for them. This one is about work
        that has already happened and is sitting on a branch.
        """
        identity = new_scratch_identity("issue-orchestrator", 6410)
        state = _state(
            _retry(f"/w/{identity.worktree_name}", identity.branch_name)
        )

        held = tech_lead_problem_artifact_hold_issue_numbers(
            state, _config(tech_lead_enabled=False)
        )

        assert 6410 in held

    def test_an_ordinary_retry_holds_nothing(self) -> None:
        """Its worktree is not disposable, so cleanup was never the threat."""
        state = _state(_retry("/w/issue-orchestrator-6410", "6410-fix-the-thing"))

        held = tech_lead_problem_artifact_hold_issue_numbers(state, _config())

        assert 6410 not in held

    def test_a_corrupt_pair_holds_nothing(self) -> None:
        """It is quarantined at admission, so nothing will resume it."""
        identity = new_scratch_identity("issue-orchestrator", 6410)
        state = _state(_retry("/w/issue-orchestrator-6410", identity.branch_name))

        held = tech_lead_problem_artifact_hold_issue_numbers(state, _config())

        assert 6410 not in held
