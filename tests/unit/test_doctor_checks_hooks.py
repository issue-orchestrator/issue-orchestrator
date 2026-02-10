"""Unit tests for doctor hook checks."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
from issue_orchestrator.infra.ai_gate_state import AiGateState, AiGateResult


def test_check_hook_verification_no_agents_reports_ai_agent_check():
    config = Config()
    config.agents = {}

    checks = hook_checks.check_hook_verification(config)

    assert any(
        c.name == "AI Agent Hooks (Installation)" and c.status == "warning"
        for c in checks
    )


class TestAiGate:
    """Tests for _check_ai_gate_report function."""

    def test_ai_gate_disabled_returns_none(self, tmp_path):
        """Test that disabled AI gate test returns None."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 0

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is None

    def test_ai_gate_skipped_when_hooks_not_ok(self, tmp_path):
        """Test AI gate test skipped when hooks not installed."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=False,
        )

        assert result is not None
        assert result.status == "info"
        assert "Skipped" in result.detail

    def test_ai_gate_fresh_shows_previous_results(self, tmp_path, monkeypatch):
        """Test that fresh AI gate test shows previous results."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Mock load_ai_gate_state to return recent check (3 days ago)
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=3),
            last_results={
                "claude-code": AiGateResult(
                    success=True,
                    message="Blocked git push",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=3),
                ),
            },
        )
        # Patch at the module where the function is imported/used
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "ok"
        assert "Passed" in result.detail
        # Verify days_ago calculation is correct (should be 3, not 0)
        assert "3d ago" in result.detail
        assert result.expandable is not None
        assert result.expandable["ran"] is False

    def test_ai_gate_cached_failure_shows_error(self, tmp_path, monkeypatch):
        """Test that cached results with failures show error status, not ok."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = False

        # Mock load_ai_gate_state to return recent check with failure
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=2),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block dangerous command",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "error"
        assert "Failed" in result.detail
        assert "2d ago" in result.detail
        assert result.expandable["ran"] is False  # type: ignore

    def test_ai_gate_cached_failure_warns_when_allowed(self, tmp_path, monkeypatch):
        """Test that cached failures show warning when dangerous_allow_failure=True."""
        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = True

        # Mock load_ai_gate_state to return recent check with failure
        recent_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=2),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="Did not block",
                    timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                ),
            },
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: recent_state,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types=set(),
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "warning"
        assert "allowed by config" in result.detail
        assert result.expandable["ran"] is False  # type: ignore

    def test_ai_gate_stale_runs_test_ai_gate(self, tmp_path, monkeypatch):
        """Test that stale AI gate test runs AI gate test."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Mock load_ai_gate_state to return stale check
        old_state = AiGateState(
            last_check=datetime.now(timezone.utc) - timedelta(days=10),
            last_results={},
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: old_state,
        )

        # Mock save_ai_gate_state
        saved_states = []
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda path, state: saved_states.append(state),
        )

        # Mock get_adapter to return an adapter with successful test_ai_gate
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked git push --no-verify")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "ok"
        assert result.expandable is not None
        assert result.expandable["ran"] is True
        assert result.expandable["triggered_by"] == "interval exceeded"
        assert len(saved_states) == 1  # State was saved

    def test_ai_gate_failure_blocks_by_default(self, tmp_path, monkeypatch):
        """Test that AI gate test failure blocks when not allowed."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = False

        # Return stale state
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock adapter that fails AI gate test
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (False, "Did not block dangerous command")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "error"
        assert "Failed" in result.detail

    def test_ai_gate_failure_warns_when_allowed(self, tmp_path, monkeypatch):
        """Test that AI gate test failure only warns when dangerous_allow_failure=True."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7
        config.hooks.ai_gate.dangerous_allow_failure = True

        # Return stale state
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock adapter that fails AI gate test
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (False, "Did not block")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.status == "warning"
        assert "allowed by config" in result.detail

    def test_ai_gate_first_run(self, tmp_path, monkeypatch):
        """Test AI gate test triggers on first run."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        # Return empty state (first run)
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        # Mock successful adapter
        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result is not None
        assert result.expandable["ran"] is True  # type: ignore
        assert result.expandable["triggered_by"] == "first run"  # type: ignore

    def test_ai_gate_expandable_details(self, tmp_path, monkeypatch):
        """Test expandable details are populated correctly."""
        from issue_orchestrator.infra.hooks.hooks import AiAgentType

        config = Config(repo_root=tmp_path)
        config.hooks.ai_gate.interval_days = 7

        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.load_ai_gate_state",
            lambda _: AiGateState(),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.save_ai_gate_state",
            lambda p, s: None,
        )

        mock_adapter = MagicMock()
        mock_adapter.supports_ai_gate.return_value = True
        mock_adapter.test_ai_gate.return_value = (True, "Blocked git push --no-verify")
        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.checks.hooks.get_adapter",
            lambda _: mock_adapter,
        )

        result = hook_checks._check_ai_gate_report(
            config=config,
            unique_types={AiAgentType.CLAUDE_CODE},
            unsupported_types=set(),
            hooks_ok=True,
        )

        assert result.expandable is not None  # type: ignore
        assert "claude-code" in result.expandable["agents_tested"]  # type: ignore
        assert result.expandable["results"]["claude-code"]["success"] is True  # type: ignore
        assert "Blocked" in result.expandable["results"]["claude-code"]["message"]  # type: ignore
