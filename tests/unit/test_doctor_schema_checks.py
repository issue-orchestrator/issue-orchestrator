"""Tests for schema-driven doctor checks."""

from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config, E2EConfig
from issue_orchestrator.infra.doctor.checks.schema import (
    format_summary,
    run_schema_checks,
)


class TestRunSchemaChecks:
    """Test the generic schema-driven check dispatcher."""

    def test_no_checks_when_conditions_not_met(self):
        """When e2e.enabled is False, path checks should be skipped."""
        cfg = Config()
        cfg.e2e = E2EConfig(enabled=False)
        checks = run_schema_checks(cfg)
        # No path check errors because e2e.enabled is False
        path_checks = [c for c in checks if "not found" in c.detail]
        assert len(path_checks) == 0

    def test_path_exists_check_passes(self, tmp_path):
        """Path check should pass when the path exists."""
        (tmp_path / "tests" / "e2e").mkdir(parents=True)
        (tmp_path / "tests" / "e2e" / "quarantine.txt").touch()

        cfg = Config()
        cfg.repo_root = tmp_path
        cfg.e2e = E2EConfig(
            enabled=True,
            quarantine_file="tests/e2e/quarantine.txt",
            pytest_args=["tests/e2e", "-v"],
        )

        checks = run_schema_checks(cfg)

        # No path-related failures
        path_failures = [c for c in checks if "not found" in c.detail]
        assert len(path_failures) == 0

    def test_path_exists_check_warns(self, tmp_path):
        """Path check should warn when the path is missing."""
        cfg = Config()
        cfg.repo_root = tmp_path
        cfg.e2e = E2EConfig(
            enabled=True,
            quarantine_file="nonexistent/quarantine.txt",
            pytest_args=["tests/e2e", "-v"],
        )

        checks = run_schema_checks(cfg)

        quarantine_checks = [c for c in checks if "quarantine" in c.detail.lower() or "Quarantine" in c.name]
        assert len(quarantine_checks) >= 1
        assert quarantine_checks[0].status == "warning"

    def test_path_checks_use_config_repo_root_not_cwd(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        """Launching from a repo subdirectory must still check target repo paths."""
        repo_root = tmp_path / "repo"
        scripts_dir = repo_root / "scripts"
        (repo_root / "tests" / "e2e").mkdir(parents=True)
        (repo_root / "tests" / "e2e" / "quarantine.txt").touch()
        scripts_dir.mkdir()

        cfg = Config()
        cfg.repo_root = repo_root
        cfg.e2e = E2EConfig(
            enabled=True,
            quarantine_file="tests/e2e/quarantine.txt",
            pytest_args=["tests/e2e", "-v"],
        )

        monkeypatch.chdir(scripts_dir)
        checks = run_schema_checks(cfg)

        assert not any("not found" in c.detail for c in checks)

    def test_references_agent_check_passes(self):
        """Agent reference check should pass when agent exists."""
        cfg = Config()
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:reviewer"
        cfg.agents = {
            "agent:reviewer": AgentConfig(prompt_path=Path("test.md")),
            "agent:backend": AgentConfig(prompt_path=Path("test.md")),
        }

        checks = run_schema_checks(cfg)
        agent_failures = [c for c in checks if "not in configured agents" in c.detail]
        assert len(agent_failures) == 0

    def test_references_agent_check_fails(self):
        """Agent reference check should fail when agent is missing."""
        cfg = Config()
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:nonexistent"
        cfg.agents = {"agent:backend": AgentConfig(prompt_path=Path("test.md"))}

        checks = run_schema_checks(cfg)
        agent_failures = [c for c in checks if "not in configured agents" in c.detail]
        assert len(agent_failures) >= 1
        assert "agent:nonexistent" in agent_failures[0].detail

    def test_references_agent_skips_none(self):
        """None agent references should not produce checks."""
        cfg = Config()
        cfg.review_enabled = True
        cfg.code_review_agent = None
        cfg.triage_review_agent = None
        cfg.e2e.issue_agent_label = None

        checks = run_schema_checks(cfg)
        agent_failures = [c for c in checks if "not in configured agents" in c.detail]
        assert len(agent_failures) == 0

    def test_disabled_e2e_skips_failure_issue_agent_warning(self):
        """Disabled E2E should not warn about the default failure issue agent label."""
        cfg = Config()
        cfg.e2e = E2EConfig(enabled=False, auto_create_issues=True)
        cfg.agents = {"agent:dev": AgentConfig(prompt_path=Path("test.md"))}

        checks = run_schema_checks(cfg)

        assert not any(check.name == "Failure issue agent label" for check in checks)


class TestFormatSummary:
    """Test the schema-driven summary formatter."""

    def test_e2e_summary(self):
        cfg = Config()
        cfg.e2e = E2EConfig(
            enabled=True,
            auto_run_interval_minutes=30,
            allow_retry_once=True,
            pytest_args=["tests/e2e", "-v"],
        )

        summary = format_summary("e2e", cfg)
        assert summary is not None
        assert "auto=30m" in summary
        assert "retry=on" in summary

    def test_e2e_summary_manual(self):
        cfg = Config()
        cfg.e2e = E2EConfig(
            enabled=True,
            auto_run_interval_minutes=0,
            allow_retry_once=False,
            pytest_args=["tests/e2e", "-v"],
        )

        summary = format_summary("e2e", cfg)
        assert summary is not None
        assert "manual" in summary
        assert "retry=off" in summary

    def test_review_summary(self):
        cfg = Config()
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:reviewer"

        summary = format_summary("review", cfg)
        assert summary is not None
        assert "default: agent:reviewer" in summary

    def test_unknown_section_returns_none(self):
        cfg = Config()
        assert format_summary("nonexistent", cfg) is None
