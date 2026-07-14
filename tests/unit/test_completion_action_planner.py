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
    CreateTriageIssueAction,
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
from issue_orchestrator.control.triage_completion import (
    triage_decision_processing_error,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.triage_manifest import PRToReview, TriageManifest
from issue_orchestrator.domain.triage_session import (
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageLaunchAuthority,
    TriageSessionFlavor,
)
from issue_orchestrator.infra.triage_authority_store import (
    SqliteTriageAuthorityStore,
)
from issue_orchestrator.ports.triage_authority import InMemoryTriageAuthorityStore
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


def make_planner(
    config: Config,
    *,
    issue_labels: list[str] | None = None,
    repository_host: RepositoryHost | None = None,
) -> CompletionActionPlanner:
    """Create a planner with a repository host that can answer label reads.

    Triage-configured tests rendezvous with ``record_authority`` through the
    SQLite adapter at ``config.repo_root`` (a tmp_path); everything else gets
    the in-memory port fake so no state files are written.
    """
    issue = SimpleNamespace(labels=issue_labels or [])
    repository_host = repository_host or cast(
        RepositoryHost, SimpleNamespace(get_issue=lambda _number: issue)
    )
    triage_authority = (
        SqliteTriageAuthorityStore.for_repo(config.repo_root)
        if config.triage_review_agent
        else InMemoryTriageAuthorityStore()
    )
    return CompletionActionPlanner(
        config, repository_host, LabelManager(config), triage_authority
    )


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


def make_triage_config(tmp_path: Path) -> Config:
    config = Config()
    config.repo_root = tmp_path  # authority store lives in the repo state dir
    config.triage_review_agent = "agent:triage"
    config.triage_reviewed_label = "triage-reviewed"
    config.triage_failed_label = "triage-failed"
    return config


def record_authority(
    config: Config, session: Session, authority: TriageLaunchAuthority
) -> None:
    """Persist the orchestrator-owned launch authority for a session run."""
    SqliteTriageAuthorityStore.for_repo(config.repo_root).record(
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
        authority=authority,
    )


def arm_batch_session(
    config: Config,
    session: Session,
    tmp_path: Path,
    *,
    with_manifest: bool = True,
) -> None:
    """Plant matching worktree copies AND the launch authority for a batch."""
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    if with_manifest:
        plant_triage_manifest(tmp_path, session)
    record_authority(
        config,
        session,
        TriageLaunchAuthority(
            flavor=TriageSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=session.issue.number,
            manifest_pr_numbers=(101, 102) if with_manifest else (),
        ),
    )


def arm_investigation_session(
    config: Config, session: Session, *, focus: int = 1
) -> None:
    """Plant matching worktree copies AND the launch authority for a focus run."""
    plant_triage_assignment(
        session,
        TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=focus,
            focus_reason="Investigate: timed out",
        ),
    )
    record_authority(
        config,
        session,
        TriageLaunchAuthority(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=session.issue.number,
            focus_issue_number=focus,
        ),
    )


def arm_health_review_session(config: Config, session: Session) -> None:
    """Plant the assignment copy AND the launch authority for a health review.

    Anchor-only scope (ADR-0031 §4): no focus issue, empty manifest set.
    """
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.HEALTH_REVIEW)
    )
    record_authority(
        config,
        session,
        TriageLaunchAuthority(
            flavor=TriageSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=session.issue.number,
        ),
    )


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
    session: Session, *, comment_targets: tuple[int, ...] = (101,)
) -> None:
    """Write a valid decision + report pair into the session's triage-data dir.

    ``comment_targets`` controls the post_comment proposals. Targets must fall
    inside the session's launch scope (manifest PRs + anchor for batch, the
    focus issue for investigations) and investigations must include the focus
    issue (#6761 F2 + re-review F2).
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
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    plant_triage_decision_pair(session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _triage_labels(actions)} == {101, 102}
    assert "in-progress" in removed_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 101
    ]
    assert len(decision_comments) == 1
    assert decision_comments[0].comment.startswith("Diagnosis for #101: flaky CI.")
    assert "ADR-0031" in decision_comments[0].comment


def test_completed_triage_session_missing_pair_fails_labels_and_surfaces_rejection(
    tmp_path: Path,
) -> None:
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
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


def test_triage_manifest_in_sibling_run_dir_is_ignored(tmp_path: Path) -> None:
    """Completion reads only ``session.run_dir`` (typed run contract).

    The pre-#6769 code scanned every run dir under the worktree's sessions
    root and could pick up a stale prior run's manifest. A manifest planted
    in a sibling run dir must now be invisible: no labels on its PRs.
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    plant_triage_assignment(
        session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
    )
    plant_triage_decision_pair(session)
    stale_run_dir = session.run_dir.parent / "20250101T000000000000Z__issue-1"
    stale_run_dir.mkdir(parents=True)
    stale_manifest = TriageManifest(
        prs=[
            PRToReview(
                number=999, title="Stale PR", url="https://example/pr/999", branch="s"
            )
        ]
    )
    stale_manifest_path = tmp_path / "stale-triage-manifest.json"
    stale_manifest.write(stale_manifest_path)
    (stale_run_dir / "manifest.json").write_text(
        json.dumps({"triage_manifest": str(stale_manifest_path)})
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    triage_label_targets = {
        action.issue_number
        for action in actions
        if isinstance(action, AddLabelAction)
        and action.label in ("triage-reviewed", "triage-failed")
    }
    assert triage_label_targets == set()


def test_completed_triage_investigation_session_plans_decision_without_labels(
    tmp_path: Path,
) -> None:
    """Failure investigations plan decision actions but never manifest labels.

    Flavor comes from the launch-time assignment (#6768 B4) — both triage
    variants share the issue-N terminal, so the manifest planted here must
    still not be labeled.
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_investigation_session(config, session)
    plant_triage_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "triage-reviewed" not in added_labels(actions)
    assert "triage-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction) and action.number == 1
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1


def test_completed_non_triage_session_is_unaffected(tmp_path: Path) -> None:
    config = make_triage_config(tmp_path)
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
    config = make_triage_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

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
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)

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
    config = make_triage_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_triage_session(tmp_path)
    arm_investigation_session(config, session)
    plant_triage_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    assert _close_actions(actions) == []
    assert _triage_failed_labels(actions) == []
    assert _triage_labels(actions) == []


def test_failure_investigation_triage_session_never_labels_manifest_prs(
    tmp_path: Path,
) -> None:
    """A focused investigation must not label PRs even when a manifest exists (#6768 B4)."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_investigation_session(config, session)
    plant_triage_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    assert "in-progress" in removed_labels(actions)


def test_completed_health_review_plans_decision_and_closes_anchor(
    tmp_path: Path,
) -> None:
    """Health review + valid pair: decision actions, close the anchor, no labels.

    The anchor issue is a walk-the-floor log entry (ADR-0031 §4) — a landed
    review closes it, and manifest labels never apply (there is no manifest).
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_health_review_session(config, session)
    # Even stray planted manifest noise must not be labeled for this flavor.
    plant_triage_manifest(tmp_path, session)
    plant_triage_decision_pair(session, comment_targets=(session.issue.number,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert "triage-reviewed" not in added_labels(actions)
    assert "triage-failed" not in added_labels(actions)
    decision_comments = [
        action for action in actions
        if isinstance(action, AddCommentAction)
        and action.number == session.issue.number
        and "Diagnosis" in action.comment
    ]
    assert len(decision_comments) == 1
    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review completed" in close.reason
    # Terminal ordering: a mid-apply crash leaves the anchor open.
    assert actions.index(close) == len(actions) - 1


def test_health_review_missing_pair_surfaces_rejection_and_keeps_anchor_open(
    tmp_path: Path,
) -> None:
    """Missing/invalid pair: rejection surfaced, anchor NOT closed (visibility)."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_health_review_session(config, session)
    # No decision artifact pair written.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    rejections = [
        action for action in actions if isinstance(action, SurfaceTriageProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_health_review_decision_targeting_anchor_passes_scope_validation(
    tmp_path: Path,
) -> None:
    """The anchor issue is the ONE allowed target for health post_comment."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_health_review_session(config, session)
    plant_triage_decision_pair(session, comment_targets=(session.issue.number,))

    error = triage_decision_processing_error(
        config,
        triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )

    assert error is None


def test_health_review_decision_targeting_other_issue_is_rejected(
    tmp_path: Path,
) -> None:
    """A health decision may not address arbitrary issues (#6761 rr F2 scope).

    Board-wide findings belong in scope-free create_issue/flag_pattern
    proposals; a post_comment outside the anchor is a contract violation on
    both completion seams (processing outcome AND planned effects).
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_health_review_session(config, session)
    plant_triage_decision_pair(session, comment_targets=(999,))

    error = triage_decision_processing_error(
        config,
        triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )
    assert error is not None
    assert "outside this session's launch scope" in error

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )
    rejections = [
        action for action in actions if isinstance(action, SurfaceTriageProposalAction)
    ]
    assert len(rejections) == 1
    assert rejections[0].mode == "rejected"
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    # And the out-of-scope comment is never planned.
    assert not any(
        isinstance(a, AddCommentAction) and a.number == 999 for a in actions
    )


@pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT])
def test_failed_health_review_closes_anchor_without_labels(
    tmp_path: Path, status: SessionStatus
) -> None:
    """FAILED/TIMED_OUT health sessions close the anchor (no manifest labels).

    An open dead anchor would be requeued at restart AND dedupe the next
    interval's trigger; closing it lets a fresh review fire on schedule.
    """
    config = make_triage_config(tmp_path)
    config.retry.interrupted_sessions.enabled = False
    session = make_triage_session(tmp_path)
    arm_health_review_session(config, session)
    plant_triage_manifest(tmp_path, session)  # planted noise: must stay unread

    actions = make_planner(config).generate_completion_actions(session, status)

    (close,) = [a for a in actions if isinstance(a, CloseIssueAction)]
    assert close.issue_number == session.issue.number
    assert "Health review session failed" in close.reason
    assert _triage_failed_labels(actions) == []
    assert _triage_labels(actions) == []
    assert actions.index(close) == len(actions) - 1


def test_triage_session_without_launch_authority_is_rejected(
    tmp_path: Path, caplog
) -> None:
    """No orchestrator launch-authority record => never trust worktree copies.

    The old fail-safe (skip effects, PRs re-enter the next batch) let a
    missing assignment reach the success path (#6761 re-review F1); now the
    rejection is surfaced and no labels are planned.
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    plant_triage_manifest(tmp_path, session)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    rejections = [
        a for a in actions
        if isinstance(a, SurfaceTriageProposalAction) and a.mode == "rejected"
    ]
    assert len(rejections) == 1
    assert "launch-authority" in rejections[0].body_preview
    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "Launch authority rejected" in caplog.text


def test_triage_artifacts_in_sibling_run_dir_are_ignored(
    tmp_path: Path, caplog
) -> None:
    """Stale artifacts from a previous run must not leak into this completion.

    Reads go exclusively through the session's typed run_dir (#6768 B6): a
    sibling run carrying a batch assignment and a full manifest produces no
    labels and no decision actions for the current session.
    """
    config = make_triage_config(tmp_path)
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
    assert "Launch authority rejected" in caplog.text


def test_successful_batch_completion_closes_tracking_issue(tmp_path: Path) -> None:
    """Batch success gives the tracking issue a crash-safe terminal state.

    Open+agent-labeled tracking issues are what startup recovery requeues and
    what _find_existing_triage_issue treats as the active batch (#6768 round
    4). Close is ordered after the PR labels so a mid-apply crash leaves the
    batch open and re-auditable. Success requires the valid decision pair.
    """
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
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
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    # No decision pair: rejection path.

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)


def test_successful_batch_without_manifest_still_closes(tmp_path: Path) -> None:
    """An empty batch (no PRs matched) must not anchor future batches forever."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path, with_manifest=False)
    plant_triage_decision_pair(session, comment_targets=())

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert any(isinstance(a, CloseIssueAction) for a in actions)


def test_failure_investigation_completion_preserves_source_issue(
    tmp_path: Path,
) -> None:
    """An investigation's anchor IS the failed work issue - never close it."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_investigation_session(config, session)
    plant_triage_decision_pair(session, comment_targets=(1,))

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert not any(isinstance(a, CloseIssueAction) for a in actions)
    assert "triage-reviewed" not in added_labels(actions)
    assert "triage-failed" not in added_labels(actions)


def _rejections(actions: tuple[object, ...]) -> list[SurfaceTriageProposalAction]:
    return [
        action for action in actions
        if isinstance(action, SurfaceTriageProposalAction) and action.mode == "rejected"
    ]


class TestFailureInvestigationDiagnosisRequired:
    """A failure investigation must publish its diagnosis to the originating
    issue via a post_comment proposal (#6761 finding 2)."""

    def test_empty_proposed_actions_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        plant_triage_decision_pair(session, comment_targets=())

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "originating issue #1" in rejection.body_preview

    def test_wrong_target_comment_is_contract_violation(self, tmp_path: Path) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        plant_triage_decision_pair(session, comment_targets=(42,))

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview

    def test_correct_target_comment_passes(self, tmp_path: Path) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
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


def _plant_decision_with_actions(session: Session, proposed: list[dict]) -> None:
    """Write a decision pair with explicit proposed actions (T1 evidence set)."""
    data_dir = session.run_dir / "triage-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "triage-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Findings and proposals.",
                "findings": [
                    {
                        "id": "T1",
                        "title": "Flaky CI",
                        "classification": "infra",
                        "evidence": ["orchestrator log lines 10-20"],
                    }
                ],
                "proposed_actions": proposed,
            }
        )
    )
    ids = ", ".join(action["id"] for action in proposed)
    (data_dir / "triage-report.md").write_text(
        f"# Report\n\nT1 leads to {ids or 'no actions'}.\n"
    )


class TestDecisionTargetScope:
    """Every targeted proposal must stay inside the immutable launch scope
    (#6761 re-review finding 2) — validated against the authority record,
    never the worktree copies."""

    def _batch(self, tmp_path: Path) -> tuple[Config, Session]:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        return config, session

    def test_in_scope_targets_pass(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 102,  # manifest PR
                    "body": "Diagnosis.",
                    "finding_ids": ["T1"],
                },
                {
                    "id": "A2",
                    "action_type": "escalate_to_human",
                    "target_number": 1,  # anchor tracking issue
                    "body": "Needs a human.",
                },
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        assert _rejections(actions) == []
        assert {a.issue_number for a in _triage_labels(actions)} == {101, 102}

    def test_out_of_scope_comment_rejected(self, tmp_path: Path) -> None:
        """A batch comment to a non-manifest PR is a confused-deputy attempt."""
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 999,
                    "body": "Out of scope.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "#999" in rejection.body_preview
        assert "outside this session's launch scope" in rejection.body_preview
        assert "triage-reviewed" not in added_labels(actions)
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_out_of_scope_escalation_rejected(self, tmp_path: Path) -> None:
        config, session = self._batch(tmp_path)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "escalate_to_human",
                    "target_number": 555,
                    "body": "Escalate elsewhere.",
                }
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview
        # The escalation must not have been planned (no needs-human label).
        assert "needs-human" not in added_labels(actions)

    @pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
    def test_out_of_scope_act_level_rejected(
        self, tmp_path: Path, act_type: str
    ) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        _plant_decision_with_actions(
            session,
            [
                {
                    "id": "A1",
                    "action_type": "post_comment",
                    "target_number": 1,
                    "body": "Diagnosis.",
                },
                {
                    "id": "A2",
                    "action_type": act_type,
                    "target_number": 777,  # not the focus issue
                    "body": "Rationale.",
                },
            ],
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED
        )

        [rejection] = _rejections(actions)
        assert "outside this session's launch scope" in rejection.body_preview


class TestLaunchScopeTamperResistance:
    """The agent-writable worktree copies carry zero authority (#6761
    re-review finding 1)."""

    def test_flavor_flip_after_launch_fails_session(self, tmp_path: Path) -> None:
        """The reviewer's repro: an investigation flipping its worktree
        assignment to batch_review must fail, never close the originating
        issue."""
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        # Agent tampering: rewrite the worktree copy to the batch flavor.
        plant_triage_assignment(
            session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
        )
        plant_triage_decision_pair(session, comment_targets=())

        error = triage_decision_processing_error(
            config,
            triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None and error.startswith(
            "triage_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(isinstance(a, CloseIssueAction) for a in actions)
        assert "triage-reviewed" not in added_labels(actions)
        assert "blocked-failed" in added_labels(actions)
        [rejection] = _rejections(actions)
        assert rejection.issue_number == session.issue.number

    def test_deleted_assignment_fails_session(self, tmp_path: Path) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        assignment_path = (
            session.run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
        )
        assignment_path.unlink()

        error = triage_decision_processing_error(
            config,
            triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None and error.startswith(
            "triage_authority: scope_tampered"
        )
        assert "missing" in error

    def test_tampered_manifest_prs_never_get_labels(self, tmp_path: Path) -> None:
        """Substituted PR numbers in the worktree manifest must not be
        labeled — labels come from the authority set, and the divergence
        itself fails the session."""
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_batch_session(config, session, tmp_path)
        # Agent tampering: substitute the manifest PR set.
        tampered = TriageManifest(
            prs=[PRToReview(number=999, title="Sub", url="https://x/999", branch="s")]
        )
        manifest_path = tmp_path / "triage-manifest.json"
        tampered.write(manifest_path)

        error = triage_decision_processing_error(
            config,
            triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )
        assert error is not None and error.startswith(
            "triage_authority: scope_tampered"
        )

        actions = make_planner(config).generate_completion_actions(
            session, SessionStatus.COMPLETED, processing_errors=[error]
        )

        assert not any(
            isinstance(a, AddLabelAction) and a.issue_number == 999 for a in actions
        )
        # The authority set still records the failure on the REAL PRs.
        assert {a.issue_number for a in _triage_failed_labels(actions)} == {101, 102}

    def test_missing_authority_is_critical_in_processing_path(
        self, tmp_path: Path
    ) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        plant_triage_assignment(
            session, TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW)
        )

        error = triage_decision_processing_error(
            config,
            triage_authority=SqliteTriageAuthorityStore.for_repo(config.repo_root),
            run_dir=session.run_dir,
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
        )

        assert error is not None and error.startswith(
            "triage_authority: missing_authority"
        )


def test_protected_agent_label_on_create_issue_rejects_decision(
    tmp_path: Path,
) -> None:
    """Untrusted agent labels may not touch workflow truth (#6761 finding 4)."""
    config = make_triage_config(tmp_path)
    session = make_triage_session(tmp_path)
    arm_batch_session(config, session, tmp_path)
    _plant_decision_with_actions(
        session,
        [
            {
                "id": "A1",
                "action_type": "create_issue",
                "title": "Follow-up",
                "body": "Fix it.",
                "labels": ["in-progress"],
                "finding_ids": ["T1"],
            }
        ],
    )

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

    def test_authority_error_prefix_is_critical(self) -> None:
        error = "triage_authority: scope_tampered: assignment flipped"
        critical, downgraded = critical_processing_errors([error])
        assert critical == [error]
        assert downgraded == []

    def test_batch_flavor_fails_manifest_and_blocks_own_issue(
        self, tmp_path: Path
    ) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_batch_session(config, session, tmp_path)

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
            "Triage completion rejected" in comment for comment in comments(actions)
        )
        assert not any(isinstance(a, CloseIssueAction) for a in actions)

    def test_investigation_flavor_blocks_own_issue_without_manifest_labels(
        self, tmp_path: Path
    ) -> None:
        config = make_triage_config(tmp_path)
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        plant_triage_manifest(tmp_path, session)  # planted noise: must stay unread

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
            "Triage completion rejected" in comment for comment in comments(actions)
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


class TestMilestoneResolutionBoundary:
    """The completion seam plans milestone INTENT only (#6769 finding 4).

    Under ``create_issue: propose`` (shadow) the decision must complete with
    ZERO GitHub reads — the reviewer reproduced a ``list_milestones`` lookup
    failure failing the completion for an issue that would never be created.
    Under execute, the name still travels as intent; the applier resolves it.
    """

    def _plant_pair_with_create_issue(self, session: Session) -> None:
        data_dir = session.run_dir / "triage-data"
        data_dir.mkdir(parents=True, exist_ok=True)
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
                    "proposed_actions": [
                        {
                            "id": "A1",
                            "action_type": "post_comment",
                            "target_number": 1,
                            "body": "Diagnosis for #1: flaky CI.",
                            "finding_ids": ["T1"],
                        },
                        {
                            "id": "A2",
                            "action_type": "create_issue",
                            "title": "Stabilize CI runner",
                            "body": "Runner disconnects mid-build.",
                            "labels": ["bug"],
                            "finding_ids": ["T1"],
                        },
                    ],
                }
            )
        )
        (data_dir / "triage-report.md").write_text(
            "# Report\n\nFinding T1: flaky CI.\n\nProposals: A1, A2.\n"
        )

    def _completed_actions(self, config: Config, session: Session, host) -> tuple:
        return make_planner(
            config, repository_host=cast(RepositoryHost, host)
        ).generate_completion_actions(session, SessionStatus.COMPLETED)

    def test_shadow_create_issue_with_explicit_milestone_makes_zero_reads(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        config = make_triage_config(tmp_path)
        config.triage.milestone_strategy.explicit = "M5"
        config.triage.authority.create_issue = "propose"
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        shadow = [
            action
            for action in actions
            if isinstance(action, SurfaceTriageProposalAction)
            and action.proposal_type == "create_issue"
        ]
        assert len(shadow) == 1 and shadow[0].mode == "shadow"
        assert not any(
            isinstance(action, CreateTriageIssueAction) for action in actions
        )

    def test_execute_create_issue_plans_name_intent_without_reads(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.actions import TriageMilestoneIntent

        config = make_triage_config(tmp_path)
        config.triage.milestone_strategy.explicit = "M5"
        assert config.triage.authority.mode_for("create_issue") == "execute"
        session = make_triage_session(tmp_path)
        arm_investigation_session(config, session)
        self._plant_pair_with_create_issue(session)
        host = MagicMock()

        actions = self._completed_actions(config, session, host)

        host.list_milestones.assert_not_called()
        [create] = [
            action for action in actions if isinstance(action, CreateTriageIssueAction)
        ]
        assert create.milestone == TriageMilestoneIntent(explicit_name="M5")
