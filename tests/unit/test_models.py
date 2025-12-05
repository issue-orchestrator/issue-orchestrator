"""Unit tests for data models."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    IssueStatus,
    AgentConfig,
    OrchestratorState,
)


class TestIssue:
    """Test the Issue data model."""

    def test_issue_creation(self):
        """Test basic issue creation."""
        issue = Issue(
            number=1,
            title="Test Issue",
            labels=["priority:high"],
            body="Test body",
        )
        assert issue.number == 1
        assert issue.title == "Test Issue"
        assert issue.body == "Test body"
        assert issue.labels == ["priority:high"]

    def test_agent_type_extraction(self):
        """Test extracting agent type from labels."""
        issue_web = Issue(
            number=1,
            title="Web task",
            labels=["agent:web", "priority:high"],
        )
        assert issue_web.agent_type == "agent:web"

        issue_mobile = Issue(
            number=2,
            title="Mobile task",
            labels=["priority:medium", "agent:mobile"],
        )
        assert issue_mobile.agent_type == "agent:mobile"

        issue_no_agent = Issue(
            number=3,
            title="No agent",
            labels=["priority:low"],
        )
        assert issue_no_agent.agent_type is None

    def test_priority_extraction(self):
        """Test priority extraction from labels."""
        high = Issue(number=1, title="High", labels=["priority:high"])
        assert high.priority == 1

        medium = Issue(number=2, title="Medium", labels=["priority:medium"])
        assert medium.priority == 2

        low = Issue(number=3, title="Low", labels=["priority:low"])
        assert low.priority == 3

        no_priority = Issue(number=4, title="No priority", labels=[])
        assert no_priority.priority == 4

    def test_priority_comparison(self):
        """Test that priority can be used for sorting."""
        issues = [
            Issue(number=1, title="Low", labels=["priority:low"]),
            Issue(number=2, title="High", labels=["priority:high"]),
            Issue(number=3, title="Medium", labels=["priority:medium"]),
        ]
        sorted_issues = sorted(issues, key=lambda i: i.priority)
        assert sorted_issues[0].number == 2  # High priority first
        assert sorted_issues[1].number == 3  # Medium second
        assert sorted_issues[2].number == 1  # Low last

    def test_priority_label(self):
        """Test human-readable priority labels."""
        high = Issue(number=1, title="High", labels=["priority:high"])
        assert high.priority_label == "high"

        medium = Issue(number=2, title="Medium", labels=["priority:medium"])
        assert medium.priority_label == "medium"

        low = Issue(number=3, title="Low", labels=["priority:low"])
        assert low.priority_label == "low"

        no_priority = Issue(number=4, title="No priority", labels=[])
        assert no_priority.priority_label == "none"

    def test_is_blocked(self):
        """Test is_blocked property."""
        blocked = Issue(number=1, title="Blocked", labels=["blocked"])
        assert blocked.is_blocked

        not_blocked = Issue(number=2, title="Not blocked", labels=[])
        assert not not_blocked.is_blocked

    def test_is_in_progress(self):
        """Test is_in_progress property."""
        in_progress = Issue(number=1, title="In progress", labels=["in-progress"])
        assert in_progress.is_in_progress

        not_in_progress = Issue(number=2, title="Not in progress", labels=[])
        assert not not_in_progress.is_in_progress

    def test_needs_human(self):
        """Test needs_human property."""
        needs_human = Issue(number=1, title="Needs human", labels=["needs-human"])
        assert needs_human.needs_human

        no_needs = Issue(number=2, title="No needs", labels=[])
        assert not no_needs.needs_human

    def test_issue_with_all_properties(self):
        """Test issue with multiple labels."""
        issue = Issue(
            number=1,
            title="Complex issue",
            labels=["agent:web", "priority:high", "needs-human"],
            milestone="M6",
            body="This needs work",
        )
        assert issue.agent_type == "agent:web"
        assert issue.priority == 1
        assert issue.needs_human
        assert issue.milestone == "M6"


class TestAgentConfig:
    """Test the AgentConfig data model."""

    def test_agent_config_creation(self, tmp_path):
        """Test basic agent config creation."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            worktree_base=tmp_path,
            model="sonnet",
            timeout_minutes=45,
        )

        assert config.prompt_path == prompt_file
        assert config.worktree_base == tmp_path
        assert config.model == "sonnet"
        assert config.timeout_minutes == 45

    def test_agent_config_defaults(self, tmp_path):
        """Test agent config with default values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
            worktree_base=tmp_path,
        )

        assert config.model == "sonnet"
        assert config.timeout_minutes == 45


class TestSession:
    """Test the Session data model."""

    def test_session_creation(self, sample_agent_config, sample_issues):
        """Test basic session creation."""
        issue = sample_issues[0]
        session = Session(
            issue=issue,
            agent_config=sample_agent_config,
            tmux_session_name="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
        )

        assert session.issue == issue
        assert session.agent_config == sample_agent_config
        assert session.tmux_session_name == "test-session"
        assert session.worktree_path == Path("/tmp/worktree")
        assert session.branch_name == "feature/test"
        assert session.status == SessionStatus.RUNNING

    def test_session_runtime_minutes(self, sample_agent_config, sample_issues):
        """Test runtime calculation."""
        issue = sample_issues[0]
        now = datetime.now()
        past = now - timedelta(minutes=30)

        session = Session(
            issue=issue,
            agent_config=sample_agent_config,
            tmux_session_name="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            started_at=past,
        )

        # Allow small variance due to test execution time
        runtime = session.runtime_minutes
        assert 29 <= runtime <= 31

    def test_session_is_timed_out_false(self, sample_agent_config, sample_issues):
        """Test is_timed_out when session is still running."""
        issue = sample_issues[0]
        now = datetime.now()
        recent = now - timedelta(minutes=10)

        session = Session(
            issue=issue,
            agent_config=sample_agent_config,
            tmux_session_name="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            started_at=recent,
        )

        assert not session.is_timed_out

    def test_session_is_timed_out_true(self, sample_agent_config, sample_issues):
        """Test is_timed_out when session exceeded timeout."""
        issue = sample_issues[0]
        config = AgentConfig(
            prompt_path=sample_agent_config.prompt_path,
            worktree_base=sample_agent_config.worktree_base,
            timeout_minutes=30,
        )

        now = datetime.now()
        old = now - timedelta(minutes=60)  # 60 minutes ago

        session = Session(
            issue=issue,
            agent_config=config,
            tmux_session_name="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            started_at=old,
        )

        assert session.is_timed_out

    def test_session_status_change(self, sample_agent_config, sample_issues):
        """Test session status transitions."""
        issue = sample_issues[0]
        session = Session(
            issue=issue,
            agent_config=sample_agent_config,
            tmux_session_name="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
        )

        assert session.status == SessionStatus.RUNNING

        session.status = SessionStatus.COMPLETED
        assert session.status == SessionStatus.COMPLETED

        session.status = SessionStatus.BLOCKED
        assert session.status == SessionStatus.BLOCKED


class TestSessionStatus:
    """Test the SessionStatus enum."""

    def test_session_status_values(self):
        """Test all session status values."""
        assert SessionStatus.PENDING.value == "pending"
        assert SessionStatus.RUNNING.value == "running"
        assert SessionStatus.COMPLETED.value == "completed"
        assert SessionStatus.BLOCKED.value == "blocked"
        assert SessionStatus.NEEDS_HUMAN.value == "needs_human"
        assert SessionStatus.FAILED.value == "failed"
        assert SessionStatus.TIMED_OUT.value == "timed_out"


class TestIssueStatus:
    """Test the IssueStatus enum."""

    def test_issue_status_values(self):
        """Test all issue status values."""
        assert IssueStatus.AVAILABLE.value == "available"
        assert IssueStatus.IN_PROGRESS.value == "in_progress"
        assert IssueStatus.BLOCKED.value == "blocked"
        assert IssueStatus.NEEDS_HUMAN.value == "needs_human"
        assert IssueStatus.COMPLETED.value == "completed"


class TestOrchestratorState:
    """Test the OrchestratorState data model."""

    def test_orchestrator_state_creation(self):
        """Test basic orchestrator state creation."""
        state = OrchestratorState()

        assert state.active_sessions == []
        assert state.completed_today == []
        assert state.paused is False
        assert state.priority_queue == []

    def test_orchestrator_state_with_data(self, sample_agent_config, sample_issues):
        """Test orchestrator state with populated data."""
        session1 = Session(
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            tmux_session_name="session-1",
            worktree_path=Path("/tmp/work1"),
            branch_name="feature/1",
        )

        state = OrchestratorState(
            active_sessions=[session1],
            completed_today=[1, 2, 3],
            paused=True,
            priority_queue=[4, 5],
        )

        assert len(state.active_sessions) == 1
        assert state.active_sessions[0] == session1
        assert state.completed_today == [1, 2, 3]
        assert state.paused is True
        assert state.priority_queue == [4, 5]

    def test_orchestrator_state_pause_toggle(self):
        """Test toggling paused state."""
        state = OrchestratorState()
        assert state.paused is False

        state.paused = True
        assert state.paused is True

        state.paused = False
        assert state.paused is False
