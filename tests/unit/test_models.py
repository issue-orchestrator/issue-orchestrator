"""Unit tests for data models."""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    IssueStatus,
    AgentConfig,
    OrchestratorState,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind


def _make_session_key(issue_number: int = 1, task: TaskKind = TaskKind.CODE) -> SessionKey:
    """Helper to create a SessionKey for testing."""
    issue_key = FakeIssueKey(name=str(issue_number))
    return SessionKey(issue=issue_key, task=task)


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
            model="sonnet",
            timeout_minutes=45,
        )

        assert config.prompt_path == prompt_file
        assert config.model == "sonnet"
        assert config.timeout_minutes == 45

    def test_agent_config_defaults(self, tmp_path):
        """Test agent config with default values."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Sample prompt")

        config = AgentConfig(
            prompt_path=prompt_file,
        )

        assert config.model == "sonnet"
        assert config.timeout_minutes == 45

    def test_get_command_legacy_includes_system_prompt_variable(self, tmp_path):
        """Test legacy command path includes {system_prompt} with completion command docs."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include completion command docs
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        # Should include instruction to read prompt file
        assert "prompt.md" in cmd

    def test_get_command_provider_always_injects_agent_done(self, tmp_path):
        """Test provider path always injects completion command docs (strict enforcement)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include completion command docs in system prompt
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "prompt.md" in cmd
        # Interactive mode: no -p, no stream-json
        assert "-p" not in cmd.split()
        assert "--output-format" not in cmd
        assert "--include-partial-messages" not in cmd

    def test_get_command_provider_appends_user_system_prompt(self, tmp_path):
        """Test user-provided system_prompt is appended, not replaced."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"system_prompt": "Custom user instructions here"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Should include BOTH completion command docs AND user content
        assert "CRITICAL" in cmd  # Completion command enforcement
        assert "coding-done" in cmd
        assert "Custom user instructions here" in cmd  # User content appended
        # Completion command docs should come BEFORE user content in the command
        critical_pos = cmd.find("CRITICAL")
        user_pos = cmd.find("Custom user instructions")
        assert critical_pos < user_pos, "Completion command docs must come before user system_prompt"

    def test_get_command_provider_user_system_prompt_cannot_replace(self, tmp_path):
        """Test user cannot replace completion command injection even with matching key."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        # User tries to provide their own system_prompt that omits completion commands
        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"system_prompt": "My own instructions without agent-done"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Completion commands MUST still be present (strict enforcement)
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        # User content is also present (extensibility)
        assert "My own instructions without agent-done" in cmd

    def test_get_command_codex_provider_injects_agent_done(self, tmp_path):
        """Test Codex provider also gets completion command injection (universal enforcement)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5-codex",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Completion commands MUST be present even for non-Claude providers
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "prompt.md" in cmd
        # Should use codex executable
        assert "codex" in cmd

    def test_get_command_non_claude_provider_prepends_to_prompt(self, tmp_path):
        """Test non-Claude providers get completion command docs prepended to prompt."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5-codex",
            initial_prompt="Do the work on issue #{issue_number}",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        # Both completion command docs and the initial prompt should be in the command
        assert "CRITICAL" in cmd
        assert "coding-done" in cmd
        assert "Do the work on issue #123" in cmd
        # Completion command docs should come before the user's initial prompt
        critical_pos = cmd.find("CRITICAL")
        user_prompt_pos = cmd.find("Do the work on issue #123")
        assert critical_pos < user_prompt_pos, "Completion command docs must precede user prompt"

    def test_get_command_review_task_uses_reviewer_default_prompt(self, tmp_path):
        """Default review prompts must not tell non-Claude reviewers to run coding-done."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Review instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="gemini",
            model="",
        )

        cmd = config.get_command(
            123,
            "Review PR #456",
            tmp_path,
            pr_number=456,
            task_kind=TaskKind.REVIEW.value,
        )

        assert "reviewer-done" in cmd
        assert "Review PR #456 for issue #123" in cmd
        assert "use coding-done to report completion" not in cmd


    def test_get_command_extra_provider_args_verbose(self, tmp_path):
        """extra_provider_args override merges onto provider_args."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(
            123, "Test Issue", tmp_path,
            extra_provider_args={"verbose": "true"},
        )
        # Check --verbose appears as an actual CLI flag (shlex-joined token)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" in tokens

    def test_get_command_extra_provider_args_none_is_safe(self, tmp_path):
        """extra_provider_args=None should work fine (default)."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" not in tokens

    def test_get_command_provider_args_verbose_from_agent_config(self, tmp_path):
        """provider_args.verbose in agent config passes --verbose."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="claude-code",
            provider_args={"verbose": "true"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)
        import shlex
        tokens = shlex.split(cmd)
        assert "--verbose" in tokens

    def test_get_command_codex_provider_args_reasoning_effort(self, tmp_path):
        """Codex provider args expose reasoning effort through normal config."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Task instructions")

        config = AgentConfig(
            prompt_path=prompt_file,
            prompt_relative="prompt.md",
            provider="codex",
            model="gpt-5.4",
            provider_args={"reasoning_effort": "xhigh"},
        )

        cmd = config.get_command(123, "Test Issue", tmp_path)

        import shlex
        tokens = shlex.split(cmd)
        assert tokens[:2] == ["codex", "exec"]
        assert "--full-auto" in tokens
        assert "gpt-5.4" in tokens
        config_idx = tokens.index("-c")
        assert tokens[config_idx + 1] == 'model_reasoning_effort="xhigh"'


class TestSession:
    """Test the Session data model."""

    def test_session_creation(self, sample_agent_config, sample_issues):
        """Test basic session creation."""
        issue = sample_issues[0]
        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
        )

        assert session.issue == issue
        assert session.agent_config == sample_agent_config
        assert session.terminal_id == "test-session"
        assert session.worktree_path == Path("/tmp/worktree")
        assert session.branch_name == "feature/test"
        assert session.status == SessionStatus.RUNNING

    def test_session_runtime_minutes(self, sample_agent_config, sample_issues):
        """Test runtime calculation."""
        issue = sample_issues[0]
        now = datetime.now()
        past = now - timedelta(minutes=30)

        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
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
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
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
            timeout_minutes=30,
        )

        now = datetime.now()
        old = now - timedelta(minutes=60)  # 60 minutes ago

        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=config,
            terminal_id="test-session",
            worktree_path=Path("/tmp/worktree"),
            branch_name="feature/test",
            started_at=old,
        )

        assert session.is_timed_out

    def test_session_status_change(self, sample_agent_config, sample_issues):
        """Test session status transitions."""
        issue = sample_issues[0]
        session = Session(
            key=_make_session_key(issue.number),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="test-session",
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
            key=_make_session_key(sample_issues[0].number),
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            terminal_id="session-1",
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
