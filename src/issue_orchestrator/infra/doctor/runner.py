"""Unified diagnostics for issue-orchestrator.

This module provides a single doctor function that both CLI and web can call.
"""

from pathlib import Path
from typing import Optional

from ..config import Config
from ...ports.command_runner import CommandRunner
from .types import DoctorResult
from .checks import (
    ai,
    clock_sync,
    config as config_checks,
    e2e,
    github,
    guardrails,
    hooks,
    milestones as milestone_checks,
    review,
    schema as schema_checks,
    workspace,
)


def run_doctor(
    config: Optional[Config] = None,
    config_path: Optional[Path] = None,
    runner: Optional[CommandRunner] = None,
) -> DoctorResult:
    """Run all diagnostic checks.

    Args:
        config: Optional pre-loaded config (used by web when orchestrator is running)
        config_path: Optional path to config file (used by CLI)
        runner: Optional CommandRunner for executing test commands (guardrails check)

    Returns:
        DoctorResult with all check results
    """
    result = DoctorResult()

    config, config_checks_list, should_stop = config_checks.load_config_with_checks(
        config, config_path
    )
    result.checks.extend(config_checks_list)
    result.checks.extend(github.check_github_auth(config))
    result.checks.extend(ai.check_ai_provider_clis())
    if should_stop or config is None:
        return result

    result.checks.extend(ai.check_ai_keys(config))

    result.checks.extend(config_checks.check_config_validation(config))
    result.checks.extend(config_checks.check_config_schema(config))
    result.checks.extend(config_checks.check_template_variables(config))
    result.checks.extend(config_checks.check_repository_config(config))
    result.checks.extend(config_checks.check_worktree_remediation(config))
    result.checks.extend(milestone_checks.check_milestone_order(config))

    result.checks.extend(workspace.check_working_directory(runner))
    result.checks.extend(workspace.check_hook_dependencies(Path.cwd()))
    result.checks.extend(hooks.check_hook_verification(config))
    result.checks.extend(hooks.check_repo_hardening(config))
    result.checks.extend(workspace.check_agents(config))

    result.checks.extend(schema_checks.run_schema_checks(config))
    result.checks.extend(review.check_code_review(config))
    result.checks.extend(e2e.check_e2e_runner(config))
    result.checks.extend(guardrails.check_guardrails(config, runner))
    result.checks.extend(clock_sync.check_clock_sync(config, runner))

    return result
