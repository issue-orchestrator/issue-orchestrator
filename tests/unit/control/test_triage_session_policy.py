"""Tests for the ADR-0031 triage session policy owner."""

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_pr_collision import NoCommitsBetweenError
from issue_orchestrator.control.triage_session_policy import (
    is_benign_triage_no_commits,
    is_triage_session,
    read_triage_assignment,
    shape_requested_actions_for_triage,
)
from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.triage_session import (
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageSessionFlavor,
)


class TestIsTriageSession:
    @pytest.mark.parametrize(
        ("triage_agent", "agent_type", "expected"),
        [
            ("agent:triage", "agent:triage", True),
            ("agent:triage", "agent:web", False),
            ("agent:triage", None, False),
            (None, "agent:triage", False),
            (None, None, False),
            ("", "agent:triage", False),
            ("", "", False),
        ],
    )
    def test_matrix(
        self, triage_agent: str | None, agent_type: str | None, expected: bool
    ) -> None:
        assert is_triage_session(triage_agent, agent_type) is expected


class TestShapeRequestedActionsForTriage:
    def test_drops_only_post_comment(self) -> None:
        requested = (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.CREATE_PR,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_triage(requested)

        assert shaped == (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

    def test_preserves_order_and_other_actions(self) -> None:
        requested = (
            RequestedAction.POST_COMMENT,
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
            RequestedAction.POST_COMMENT,
        )

        shaped = shape_requested_actions_for_triage(requested)

        assert shaped == (
            RequestedAction.PUSH_BRANCH,
            RequestedAction.ADD_BLOCKED_LABEL,
        )

    def test_no_post_comment_is_identity(self) -> None:
        requested = (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR)

        assert shape_requested_actions_for_triage(requested) == requested


class TestIsBenignTriageNoCommits:
    def test_true_only_for_create_pr_with_no_commits_error(self) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_triage_no_commits(RequestedAction.CREATE_PR, error) is True

    @pytest.mark.parametrize(
        "action",
        [a for a in RequestedAction if a is not RequestedAction.CREATE_PR],
    )
    def test_false_for_other_actions(self, action: RequestedAction) -> None:
        error = NoCommitsBetweenError(base="main", head="issue-1")

        assert is_benign_triage_no_commits(action, error) is False

    def test_false_for_other_errors_on_create_pr(self) -> None:
        assert (
            is_benign_triage_no_commits(
                RequestedAction.CREATE_PR, RuntimeError("boom")
            )
            is False
        )


class TestReadTriageAssignment:
    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert read_triage_assignment(tmp_path) is None

    def test_reads_assignment_from_triage_data(self, tmp_path: Path) -> None:
        assignment = TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=99,
            focus_reason="hang",
        )
        assignment.write(tmp_path / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME)

        assert read_triage_assignment(tmp_path) == assignment

    def test_malformed_content_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "flavor": "bogus"}')

        with pytest.raises(ValueError, match="flavor"):
            read_triage_assignment(tmp_path)
