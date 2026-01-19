"""Workspace and agent checks for doctor."""

import shutil
from pathlib import Path

from ..types import Check
from ...config import Config
from ....ports.command_runner import CommandRunner


def check_working_directory(runner: CommandRunner | None) -> list[Check]:
    checks: list[Check] = []
    if runner is None:
        return checks

    repo_root = Path.cwd()
    try:
        from ....adapters.git.git_cli import GitCLI
        git = GitCLI(runner=runner)
        status_result = git.run(repo_root, ["status", "--porcelain"], timeout_s=10, check=False)
        if status_result.returncode == 0:
            has_uncommitted = bool(status_result.stdout.strip())
            if has_uncommitted:
                checks.append(Check(
                    name="Working Directory",
                    status="warning",
                    detail="Uncommitted changes (won't affect agent worktrees - they branch from main)",
                ))
            else:
                checks.append(Check(
                    name="Working Directory",
                    status="ok",
                    detail="Clean",
                ))
        else:
            checks.append(Check(
                name="Working Directory",
                status="info",
                detail="Could not check git status",
            ))
    except Exception:
        checks.append(Check(
            name="Working Directory",
            status="info",
            detail="Could not check git status",
        ))

    return checks


def check_hook_dependencies(repo_root: Path) -> list[Check]:
    checks: list[Check] = []

    python3 = shutil.which("python3")
    if python3:
        checks.append(Check(
            name="Python3",
            status="ok",
            detail=python3,
        ))
    else:
        checks.append(Check(
            name="Python3",
            status="error",
            detail="python3 not found in PATH (required for hooks)",
        ))

    return checks


def _check_agent_scripts(config: Config) -> Check:
    """Check if agent scripts are available."""
    missing_scripts = []
    for name, agent_cfg in config.agents.items():
        cmd_parts = agent_cfg.command.split()
        script = None
        for part in cmd_parts:
            if "=" not in part or part.startswith("{"):
                script = part
                break
        if script and not shutil.which(script) and not Path(script).exists():
            missing_scripts.append(f"{name}: {script}")

    if missing_scripts:
        return Check(
            name="Agent Scripts",
            status="error",
            detail=f"Missing: {', '.join(missing_scripts[:3])}" + ("..." if len(missing_scripts) > 3 else ""),
        )
    return Check(name="Agent Scripts", status="ok", detail="All found")


def _check_retry_templates(config: Config) -> Check | None:
    """Check if retry templates exist. Returns None if no templates configured."""
    repo_root = Path.cwd()
    missing_templates = []

    if config.retry and config.retry.retry_prompt_template:
        template_path = repo_root / config.retry.retry_prompt_template
        if not template_path.exists():
            missing_templates.append(f"retry: {config.retry.retry_prompt_template}")

    for name, agent_cfg in config.agents.items():
        if agent_cfg.retry_prompt_template:
            template_path = repo_root / agent_cfg.retry_prompt_template
            if not template_path.exists():
                missing_templates.append(f"{name}: {agent_cfg.retry_prompt_template}")

    if missing_templates:
        return Check(
            name="Retry Templates",
            status="error",
            detail=f"Missing: {', '.join(missing_templates[:3])}" + ("..." if len(missing_templates) > 3 else ""),
        )
    if config.retry and config.retry.retry_prompt_template:
        return Check(name="Retry Templates", status="ok", detail="All found")
    return None


def check_agents(config: Config) -> list[Check]:
    checks: list[Check] = []
    agent_count = len(config.agents)

    if agent_count == 0:
        checks.append(Check(name="Agents", status="warning", detail="None configured"))
        return checks

    checks.append(Check(name="Agents", status="ok", detail=f"{agent_count} configured"))
    checks.append(_check_agent_scripts(config))

    template_check = _check_retry_templates(config)
    if template_check:
        checks.append(template_check)

    return checks
