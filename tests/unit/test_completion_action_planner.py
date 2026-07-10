"""Direct tests for completion action planning policy."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
    SurfaceTriageProposalAction,
)
from issue_orchestrator.control.completion_action_planner import (
    CompletionActionPlanner,
    critical_processing_errors,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUSH,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.triage_manifest import PRToReview, TriageManifest
from issue_orchestrator.domain.triage_session import (
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import RepositoryHost
from tests.unit.session_run_helpers import make_session_run_assets


def make_issue(
    number: int = 1,
    *,
    labels: list[str] | None = None,
) -> Issue:
    """Create an issue for planner tests."""
    return Issue(
        number=number,
        title=f"Test issue {number}",
        labels=labels or ["agent:test"],
        repo="owner/repo",
    )


def make_session(
    tmp_path: Path,
    *,
    issue: Issue | None = None,
    terminal_id: str = "issue-1",
) -> Session:
    """Create a session for planner tests."""
    issue = issue or make_issue()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md", timeout_minutes=45),
        terminal_id=terminal_id,
        worktree_path=tmp_path,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(tmp_path, session_name=terminal_id),
    )


def make_planner(config: Config, *, issue_labels: list[str] | None = None) -> CompletionActionPlanner:
    """Create a planner with a repository host that can answer label reads."""
    issue = SimpleNamespace(labels=issue_labels or [])
    repository_host = cast(RepositoryHost, SimpleNamespace(get_issue=lambda _number: issue))
    return CompletionActionPlanner(config, repository_host, LabelManager(config))


def added_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels added by a planner result."""
    return {action.label for action in actions if isinstance(action, AddLabelAction)}


def removed_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels removed by a planner result."""
    return {action.label for action in actions if isinstance(action, RemoveLabelAction)}


def comments(actions: tuple[object, ...]) -> list[str]:
    """Return comments emitted by a planner result."""
    return [action.comment for action in actions if isinstance(action, AddCommentAction)]


def test_timeout_issue_session_marks_blocked_failed_and_releases_claim(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.TIMED_OUT,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Timed Out" in comment for comment in comments(actions))


def test_failed_issue_session_without_retry_needs_human(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = False

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert "needs-human" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Needs Investigation" in comment for comment in comments(actions))


def test_blocked_issue_session_uses_reported_label_and_reason(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.BLOCKED,
        blocked_label="blocked-upstream",
        blocked_reason="Waiting on dependency",
    )

    assert "blocked-upstream" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Waiting on dependency" in comment for comment in comments(actions))


def test_completed_with_publish_error_tracks_publish_failure(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        processing_errors=[f"{ERROR_PREFIX_PUSH}: rejected"],
        diagnostic_path=".issue-orchestrator/diagnostics/publish.md",
    )

    assert {"publish-failed", "publish-fail-count-1"} <= added_labels(actions)
    assert {"in-progress", "needs-rework"} <= removed_labels(actions)
    assert any("Publishing Failed" in comment for comment in comments(actions))


def test_review_exchange_halt_puts_issue_on_hold(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Review Exchange Halted" in comment for comment in comments(actions))


def make_triage_config() -> Config:
    config = Config()
    config.triage_review_agent = "agent:triage"
    config.triage_reviewed_label = "triage-reviewed"
    config.triage_failed_label = "triage-failed"
    return config


def make_triage_session(tmp_path: Path, *, terminal_id: str = "issue-1") -> Session:
    issue = make_issue(labels=["agent:triage"])  # agent_type derives from labels
    return make_session(tmp_path, issue=issue, terminal_id=terminal_id)


def plant_triage_assignment(session: Session, assignment: TriageAssignment) -> None:
    """Write the launch-time assignment into the session's triage-data dir."""
    assignment_path = session.run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
    assignment.write(assignment_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["triage_assignment"] = str(assignment_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def plant_triage_manifest(tmp_path: Path, session: Session) -> None:
    """Write a two-PR triage manifest discoverable via the run manifest."""
    manifest = TriageManifest(
        prs=[
            PRToReview(number=101, title="PR 101", url="https://example/pr/101", branch="b1"),
            PRToReview(number=102, title="PR 102", url="https://example/pr/102", branch="b2"),
        ]
    )
    manifest_path = tmp_path / "triage-manifest.json"
    manifest.write(manifest_path)
    run_manifest_path = session.run_dir / "manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text())
    run_manifest["triage_manifest"] = str(manifest_path)
    run_manifest_path.write_text(json.dumps(run_manifest))


def plant_triage_decision_pair(
    session: Session, *, comment_targets: tuple[int, ...] = (42,)
) -> None:
    """Write a valid decision + report pair into the session's triage-data dir.

    ``comment_targets`` controls the post_comment proposals; failure
    investigations must include the assignment's focus issue (#6761 F2).
    """
    data_dir = session.run_dir / "triage-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    proposed_actions = [
        {
            "id": f"A{index}",
            "action_type": "post_comment",
            "target_number": target,
            "body": f"Diagnosis for #{target}: flaky CI.",
            "finding_ids": ["T1"],
        }
        for index, target in enumerate(comment_targets, start=1)
    ]
    (data_dir / "triage-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "One systemic pattern found.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed_actions,
            }
        )
    )
    action_ids = ", ".join(action["id"] for action in proposed_actions)
    (data_dir / "triage-report.md").write_text(
        f"# Report\n\nT1 leads to {action_ids or 'no actions'}.\n"
    )


def _triage_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "triage-reviewed"
    ]


def test_completed_triage_session_labels_manifest_prs_and_plans_decision(
    tmp_path: Path,
) -> None:
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)
    plant_triage_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _triage_labels(actions)} == {101, 102}
    assert "in-progress" in removed_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 42
    ]
    assert len(decision_comments) == 1
    assert decision_comments[0].comment.startswith("Diagnosis for #42: flaky CI.")
    assert "ADR-0031" in decision_comments[0].comment


def test_completed_triage_session_missing_pair_fails_labels_and_surfaces_rejection(
    tmp_path: Path,
) -> None:
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)
    # No decision artifact pair written: contract violation, no grace path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    failed_actions = [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "triage-failed"
    ]
    assert {action.issue_number for action in failed_actions} == {101, 102}
    assert "triage-reviewed" not in added_labels(actions)
    rejections = [
        action for action in actions if isinstance(action, SurfaceTriageProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert rejections[0].proposal_type == "decision"
    assert rejections[0].issue_number == session.issue.number
    assert "triage-decision.json" in rejections[0].body_preview


def test_completed_triage_investigation_session_plans_decision_without_labels(
    tmp_path: Path,
) -> None:
    """Failure investigations plan decision actions but never manifest labels.

    Flavor comes from the launch-time assignment (#6768 B4) — both triage
    variants share the issue-N terminal, so the manifest planted here must
    still not be labeled.
    """
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=1,
            focus_reason="Investigate: timed out",
        ),
    )
    plant_triage_manifest(tmp_path, session)
    plant_triage_decision_pair(session, comment_targets=(1, 42))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "triage-reviewed" not in added_labels(actions)
    assert "triage-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 42
    ]
    assert len(decision_comments) == 1


def test_completed_non_triage_session_is_unaffected(tmp_path: Path) -> None:
    config = make_triage_config()
    session = make_session(tmp_path)  # agent:test, not the triage agent

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, SurfaceTriageProposalAction) for a in actions)
    assert added_labels(actions) == set()
    assert "in-progress" in removed_labels(actions)


def _close_actions(actions: tuple[object, ...]) -> list[CloseIssueAction]:
    return [a for a in actions if isinstance(a, CloseIssueAction)]


def _triage_failed_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "triage-failed"
    ]


def test_failed_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A FAILED batch reaches the triage-failed contract (#6768 r5).

    Manifest PRs carry the operator-visible triage-failed label and the
    tracking issue closes (after the generic needs-human diagnosis and the PR
    labels) so restart recovery cannot requeue it with an empty manifest.
    """
    config = make_triage_config()
    config.retry.interrupted_sessions.enabled = False
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.FAILED,
    )

    assert {a.issue_number for a in _triage_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with (not replaces) the generic failure diagnosis...
    assert "needs-human" in added_labels(actions)
    # ...and the terminal close comes after every label action.
    assert actions.index(close) == len(actions) - 1


def test_timed_out_batch_labels_prs_failed_and_closes_tracking_issue(
    tmp_path: Path,
) -> None:
    """A TIMED_OUT batch gets the same terminal lifecycle as a failed one."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.TIMED_OUT,
    )

    assert {a.issue_number for a in _triage_failed_labels(actions)} == {101, 102}
    (close,) = _close_actions(actions)
    assert close.issue_number == session.issue.number
    # Composes with the generic timeout diagnosis; close is last.
    assert "blocked-failed" in added_labels(actions)
    assert actions.index(close) == len(actions) - 1


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failure_investigation_failure_paths_preserve_source_issue(
    tmp_path: Path, status: SessionStatus
) -> None:
    """Failed/timed-out investigations never touch manifest PRs or close their
    anchor — it IS the original failed work issue (#6768 r5 controls)."""
    config = make_triage_config()
    config.retry.interrupted_sessions.enabled = False
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=1,
            focus_reason="Investigate: timed out",
        ),
    )
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(session, status)

    assert _close_actions(actions) == []
    assert _triage_failed_labels(actions) == []
    assert _triage_labels(actions) == []


def test_failure_investigation_triage_session_never_labels_manifest_prs(
    tmp_path: Path,
) -> None:
    """A focused investigation must not label PRs even when a manifest exists (#6768 B4)."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=1,
            focus_reason="Investigate: timed out",
        ),
    )
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    assert "in-progress" in removed_labels(actions)


def test_triage_session_without_assignment_skips_labels_and_warns(
    tmp_path: Path, caplog
) -> None:
    """Pre-upgrade sessions fail safe: no labels, PRs re-enter the next batch."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    assert not any(isinstance(a, SurfaceTriageProposalAction) for a in actions)
    assert "in-progress" in removed_labels(actions)
    assert "No triage assignment" in caplog.text


def test_triage_artifacts_in_sibling_run_dir_are_ignored(
    tmp_path: Path, caplog
) -> None:
    """Stale artifacts from a previous run must not leak into this completion.

    Reads go exclusively through the session's typed run_dir (#6768 B6): a
    sibling run carrying a batch assignment and a full manifest produces no
    labels and no decision actions for the current session.
    """
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    sibling = session.run_dir.parent / "issue-1__coding-0"
    (sibling / "triage-data").mkdir(parents=True)
    TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW).write(
        sibling / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
    )
    manifest = TriageManifest(
        prs=[PRToReview(number=301, title="Stale", url="https://example/pr/301", branch="s1")]
    )
    manifest_path = sibling / "triage-manifest.json"
    manifest.write(manifest_path)
    (sibling / "manifest.json").write_text(
        json.dumps({"triage_manifest": str(manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, AddLabelAction) and a.label.startswith("triage-") for a in actions)
    assert "No triage assignment" in caplog.text


def test_successful_batch_completion_closes_tracking_issue(tmp_path: Path) -> None:
    """Batch success gives the tracking issue a crash-safe terminal state.

    Open+agent-labeled tracking issues are what startup recovery requeues and
    what _find_existing_triage_issue treats as the active batch (#6768 round
    4). Close is ordered after the PR labels so a mid-apply crash leaves the
    batch open and re-auditable. Success requires the valid decision pair.
    """
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)
    plant_triage_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    closes = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert [c.issue_number for c in closes] == [session.issue.number]
    label_indexes = [
        i for i, a in enumerate(actions)
        if isinstance(a, AddLabelAction) and a.label == "triage-reviewed"
    ]
    assert label_indexes and actions.index(closes[0]) > max(label_indexes)


def test_batch_completion_with_rejected_pair_does_not_close_tracking_issue(
    tmp_path: Path,
) -> None:
    """A contract violation leaves the batch anchor open for re-audit."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_manifest(tmp_path, session)
    # No decision pair: rejection path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_successful_batch_without_manifest_still_closes(tmp_path: Path) -> None:
    """An empty batch (no PRs matched) must not anchor future batches forever."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert any(isinstance(a, CloseIssueAction) for a in actions)


def test_failure_investigation_completion_preserves_source_issue(
    tmp_path: Path,
) -> None:
    """An investigation's anchor IS the failed work issue - never close it."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=1,
            focus_reason="Investigate: timed out",
        ),
    )
    plant_triage_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "triage-reviewed" not in added_labels(actions)
    assert "triage-failed" not in added_labels(actions)


def _plant_investigation_assignment(session: Session, focus: int = 1) -> None:
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=focus,
            focus_reason="Investigate: timed out",
        ),
    )


def _rejections(actions: tuple[object, ...]) -> list[SurfaceTriageProposalAction]:
    return [
        action for action in actions
        if isinstance(action, SurfaceTriageProposalAction) and action.mode == "rejected"
    ]


class TestFailureInvestigationDiagnosisRequired:
    """A failure investigation must publish its diagnosis to the originating
    issue via a post_comment proposal (#6761 finding 2)."""

    def test_empty_proposed_actions_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_triage_config()
        session = make_triage_session(tmp_path)
        _plant_investigation_assignment(session)
        plant_triage_decision_pair(session, comment_targets=())

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "originating issue #1" in rejection.body_preview

    def test_wrong_target_comment_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_triage_config()
        session = make_triage_session(tmp_path)
        _plant_investigation_assignment(session)
        plant_triage_decision_pair(session, comment_targets=(42,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "originating issue #1" in rejection.body_preview

    def test_correct_target_comment_passes(self, tmp_path: Path) -> None:
        config = make_triage_config()
        session = make_triage_session(tmp_path)
        _plant_investigation_assignment(session)
        plant_triage_decision_pair(session, comment_targets=(1,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        diagnosis = [
            action for action in actions
            if isinstance(action, AddCommentAction) and action.number == 1
        ]
        assert diagnosis and "Diagnosis" in diagnosis[0].comment


def test_protected_agent_label_on_create_issue_rejects_decision(
    tmp_path: Path,
) -> None:
    """Untrusted agent labels may not touch workflow truth (#6761 finding 4)."""
    config = make_triage_config()
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    data_dir = session.run_dir / "triage-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "triage-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "One pattern.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["log tail"],
                    }
                ],
                "proposed_actions": [
                    {
                        "id": "A1",
                        "action_type": "create_issue",
                        "title": "Follow-up",
                        "body": "Fix it.",
                        "labels": ["in-progress"],
                        "finding_ids": ["T1"],
                    }
                ],
            }
        )
    )
    (data_dir / "triage-report.md").write_text("# Report\n\nT1 leads to A1.\n")

    actions = make_planner(config).generate_completion_actions(
        session, SessionStatus.COMPLETED
    )

    [rejection] = _rejections(actions)
    assert "protected" in rejection.body_preview
    assert "in-progress" in rejection.body_preview
    assert not any(
        isinstance(a, AddLabelAction) and a.label == "triage-reviewed" for a in actions
    )


class TestTriageDecisionFailureTransition:
    """A rejected pair rides the critical-error seam: FAILED history plus the
    blocked/failed labeling path for the session's own issue (#6761 finding 3)."""

    ERROR = "triage_decision: contract_violation: finding T1 has no evidence"

    def test_error_prefix_is_critical(self) -> None:
        critical, downgraded = critical_processing_errors([self.ERROR])
        assert critical == [self.ERROR]
        assert downgraded == []

    def test_batch_flavor_fails_manifest_and_blocks_own_issue(
        self, tmp_path: Path
    ) -> None:
        config = make_triage_config()
        session = make_triage_session(tmp_path)
        plant_triage_assignment(
            session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
        )
        plant_triage_manifest(tmp_path, session)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        failed = [
            a for a in actions
            if isinstance(a, AddLabelAction) and a.label == "triage-failed"
        ]
        assert {a.issue_number for a in failed} == {101, 102}
        assert "blocked-failed" in added_labels(actions)
        assert "publish-failed" not in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert "finding T1 has no evidence" in rejection.body_preview
        assert any(
            "Triage decision rejected" in comment for comment in comments(actions)
        )
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_investigation_flavor_blocks_own_issue_without_manifest_labels(
        self, tmp_path: Path
    ) -> None:
        config = make_triage_config()
        session = make_triage_session(tmp_path)
        _plant_investigation_assignment(session)
        plant_triage_manifest(tmp_path, session)

        actions = make_planner(config).generate_completion_actions(
            session,
            SessionStatus.COMPLETED,
            processing_errors=[self.ERROR],
        )

        assert "triage-failed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        assert "in-progress" in removed_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number
        assert any(
            "Triage decision rejected" in comment for comment in comments(actions)
        )


def test_interrupted_retry_adds_guard_and_keeps_retry_loop_bounded(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = True
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert config.retry.interrupted_sessions.coding_guard_label in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Interrupted" in comment for comment in comments(actions))


def test_create_pr_error_is_downgraded_when_pr_exists(caplog) -> None:
    critical, downgraded = critical_processing_errors(
        [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"],
        pr_url="https://github.com/owner/repo/pull/5",
        issue_number=5,
        log_downgraded=True,
        context="test",
    )

    assert critical == []
    assert downgraded == [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"]
    assert "Ignoring non-blocking create_pr processing errors" in caplog.text
