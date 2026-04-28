"""Unified diagnostics for issue-orchestrator.

This module provides a single doctor function that both CLI and web can call.
"""

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from ..ports.command_runner import CommandRunner
from .config import Config
from .doctor.types import Check, DoctorResult
from .sqlite_maintenance import BackupStatus, quick_check_db, get_backup_statuses
from .sqlite_registry import list_sqlite_databases


def _check_guardrails_in_worktree(
    repo_root: Path,
    runner: CommandRunner,
    worktree_base: str = "../",
    base_branch: str | None = None,
) -> list[Check]:
    """Create a test worktree and verify guardrails work.

    This tests the actual agent environment by:
    1. Creating a worktree using normal setup
    2. Running guardrail checks with the PATH as agents see it
    3. Cleaning up the worktree

    Args:
        repo_root: Path to the repository root
        runner: CommandRunner for executing test commands
        worktree_base: Base directory for worktrees

    Returns list of Check results for each guardrail tested.
    """
    from ..adapters.worktree._worktree import create_worktree, remove_worktree
    from .doctor.checks.guardrails import check_guardrails_in_worktree_impl

    checks: list[Check] = []
    worktree_path: Optional[Path] = None

    try:
        # Create test worktree
        worktree_path, _, _, _, _, _, _ = create_worktree(
            repo_root=repo_root,
            issue_number=0,
            issue_title="doctor-guardrail-test",
            worktree_base=Path(worktree_base),
            base_branch=base_branch,
        )

        checks.append(Check(
            name="Test Worktree",
            status="ok",
            detail=f"Created at {worktree_path}",
        ))

        # Delegate to implementation that performs the actual guardrail checks
        checks.extend(check_guardrails_in_worktree_impl(worktree_path, runner))

    except Exception as e:
        checks.append(Check(
            name="Test Worktree",
            status="error",
            detail=f"Failed to create: {e}",
        ))
    finally:
        # Cleanup
        if worktree_path and worktree_path.exists():
            try:
                remove_worktree(worktree_path)
            except Exception:
                pass  # Best effort cleanup

    return checks


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

    config = _check_config(result, config, config_path)
    # Run modular check sections
    _check_github_auth(result, config)
    _check_ai_providers(result)

    if config is None:
        return result

    _check_repository(result, config)
    _check_worktree_setup(result, config)
    _check_working_directory(result, runner)
    _check_hook_dependencies(result)
    _check_agents(result, config, runner)
    _check_code_review(result, config)
    _check_review_exchange(result, config, runner)
    _check_e2e_runner(result, config)
    _check_sqlite_databases(result, config)
    _check_sqlite_backups(result, config)
    _check_guardrails(result, config, runner)

    return result


def _check_github_auth(result: DoctorResult, config: Optional[Config]) -> None:
    """Check GitHub authentication status."""
    from .doctor.checks.github import check_github_auth

    result.checks.extend(check_github_auth(config))


def _check_ai_providers(result: DoctorResult) -> None:
    """Check AI provider keys and CLIs."""
    from .ai_keys import list_ai_keys, AI_PROVIDERS
    from issue_orchestrator.agent_runner import list_providers, get_provider

    # Check AI keys
    ai_keys = list_ai_keys()
    configured = [(k, s) for k, (_, s) in ai_keys.items() if s != "not set"]

    if configured:
        detail_parts = []
        for key_name, source in configured:
            provider_name = AI_PROVIDERS.get(key_name, {}).get("name", key_name)
            detail_parts.append(f"{provider_name} ({source})")
        result.checks.append(Check(
            name="AI Provider Keys",
            status="ok",
            detail=", ".join(detail_parts),
        ))
    else:
        result.checks.append(Check(
            name="AI Provider Keys",
            status="warning",
            detail="No AI keys configured. Run: issue-orchestrator keys set <provider>",
        ))

    # Check AI CLIs
    providers = list_providers()
    available_providers = []
    missing_providers = []

    for name in providers:
        provider = get_provider(name)
        if provider.is_available():
            version = provider.check_version()
            version_info = f" ({version})" if version else ""
            available_providers.append(f"{name}{version_info}")
        else:
            missing_providers.append(name)

    if available_providers:
        result.checks.append(Check(
            name="AI Provider CLIs",
            status="ok",
            detail=", ".join(available_providers),
        ))
    else:
        result.checks.append(Check(
            name="AI Provider CLIs",
            status="error",
            detail=f"No CLIs installed. Install one of: {', '.join(providers)}",
        ))

    if missing_providers:
        result.checks.append(Check(
            name="AI Provider CLIs (Missing)",
            status="info",
            detail=f"Not installed: {', '.join(missing_providers)}",
        ))


def _check_config(
    result: DoctorResult,
    config: Optional[Config],
    config_path: Optional[Path],
) -> Optional[Config]:
    """Load and validate configuration. Returns config if successful."""
    if config is None and config_path:
        if config_path.exists():
            try:
                config = Config.load(config_path)
                result.checks.append(Check(
                    name="Config File",
                    status="ok",
                    detail=str(config_path),
                ))
            except Exception as e:
                result.checks.append(Check(
                    name="Config File",
                    status="error",
                    detail=f"Failed to load: {e}",
                ))
                return None
        else:
            result.checks.append(Check(
                name="Config File",
                status="warning",
                detail="Not found",
            ))
            return None

    if config is None:
        config = _try_find_config(result)
        if config is None:
            return None

    # Validate config
    _validate_config_schema(result, config)

    return config


def _try_find_config(result: DoctorResult) -> Optional[Config]:
    """Try to find config in new location."""
    from .config import list_configs, get_config_path

    cwd = Path.cwd()
    available = list_configs(cwd)
    if available:
        config_file = get_config_path(cwd, available[0])
        try:
            config = Config.load(config_file)
            result.checks.append(Check(
                name="Config File",
                status="ok",
                detail=str(config_file.relative_to(cwd)),
            ))
            return config
        except Exception as e:
            result.checks.append(Check(
                name="Config File",
                status="error",
                detail=f"Failed to load {config_file}: {e}",
            ))
            return None
    else:
        result.checks.append(Check(
            name="Config File",
            status="warning",
            detail="Not found in current directory",
        ))
        return None


def _validate_config_schema(result: DoctorResult, config: Config) -> None:
    """Validate config schema and template variables."""
    validation_errors = config.validate()
    if validation_errors:
        result.checks.append(Check(
            name="Config Validation",
            status="error",
            detail="; ".join(validation_errors[:3]) + ("..." if len(validation_errors) > 3 else ""),
        ))
    else:
        result.checks.append(Check(
            name="Config Validation",
            status="ok",
            detail="All checks passed",
        ))

    # Check for unknown fields in config schema
    unknown_fields = config.validate_unknown_fields()
    if unknown_fields:
        field_names = [f[0] for f in unknown_fields]
        detail = ", ".join(field_names[:5]) + ("..." if len(field_names) > 5 else "")
        result.checks.append(Check(
            name="Config Schema",
            status="error",
            detail=f"Unknown fields: {detail}",
        ))
    else:
        result.checks.append(Check(
            name="Config Schema",
            status="ok",
            detail="No unknown fields",
        ))

    # Check for invalid template variables
    invalid_templates = config.validate_template_variables()
    if invalid_templates:
        details = []
        for agent_label, field_name, bad_vars in invalid_templates[:3]:
            details.append(f"{agent_label}.{field_name}: {{{', '.join(sorted(bad_vars))}}}")
        detail = "; ".join(details) + ("..." if len(invalid_templates) > 3 else "")
        result.checks.append(Check(
            name="Template Variables",
            status="error",
            detail=f"Invalid: {detail}",
        ))
    else:
        result.checks.append(Check(
            name="Template Variables",
            status="ok",
            detail="All template variables valid",
        ))


def _check_repository(result: DoctorResult, config: Config) -> None:
    """Check repository configuration."""
    if config.repo:
        result.checks.append(Check(
            name="Repository",
            status="ok",
            detail=config.repo,
        ))
    else:
        result.checks.append(Check(
            name="Repository",
            status="warning",
            detail="Not configured",
        ))


def _check_worktree_setup(result: DoctorResult, config: Config) -> None:
    """Check worktree setup readiness for foreign repos."""
    import sys

    # Check coding-done and reviewer-done are available from the orchestrator's venv
    venv_bin = Path(sys.executable).parent
    for tool_name in ("coding-done", "reviewer-done"):
        tool_bin = venv_bin / tool_name
        if tool_bin.exists():
            result.checks.append(Check(
                name=tool_name,
                status="ok",
                detail=f"Found at {tool_bin}",
            ))
        else:
            result.checks.append(Check(
                name=tool_name,
                status="error",
                detail=(
                    f"Not found at {tool_bin}. "
                    f"Agents need {tool_name} to report completion. "
                    "Reinstall the orchestrator: pip install -e '.[dev]'"
                ),
            ))

    # Check worktree setup commands
    if config.setup_worktree:
        cmds = ", ".join(config.setup_worktree)
        result.checks.append(Check(
            name="Worktree Setup",
            status="ok",
            detail=f"Commands configured: {cmds}",
        ))
    else:
        result.checks.append(Check(
            name="Worktree Setup",
            status="info",
            detail=(
                "No worktree setup commands configured. "
                "If your repo needs dependency installation after worktree creation, "
                "add commands under worktrees.setup in config."
            ),
        ))


def _check_working_directory(result: DoctorResult, runner: Optional[CommandRunner]) -> None:
    """Check for uncommitted changes in working directory."""
    if not runner:
        return

    repo_root = Path.cwd()
    try:
        from ..adapters.git.git_cli import GitCLI
        git = GitCLI(runner=runner)
        status_result = git.run(repo_root, ["status", "--porcelain"], timeout_s=10, check=False)
        if status_result.returncode == 0:
            has_uncommitted = bool(status_result.stdout.strip())
            if has_uncommitted:
                result.checks.append(Check(
                    name="Working Directory",
                    status="warning",
                    detail="Uncommitted changes (won't affect agent worktrees - they branch from main)",
                ))
            else:
                result.checks.append(Check(
                    name="Working Directory",
                    status="ok",
                    detail="Clean",
                ))
        else:
            result.checks.append(Check(
                name="Working Directory",
                status="info",
                detail="Could not check git status",
            ))
    except Exception:
        result.checks.append(Check(
            name="Working Directory",
            status="info",
            detail="Could not check git status",
        ))


def _check_hook_dependencies(result: DoctorResult) -> None:
    """Check hook dependencies (python3, hook helper)."""
    python3 = shutil.which("python3")
    if python3:
        result.checks.append(Check(
            name="Python3",
            status="ok",
            detail=python3,
        ))
    else:
        result.checks.append(Check(
            name="Python3",
            status="error",
            detail="python3 not found in PATH (required for hooks)",
        ))

    hook_helper = Path.cwd() / "tools" / "hooks" / "allow_git_push.py"
    if hook_helper.exists():
        result.checks.append(Check(
            name="Hook Helper",
            status="ok",
            detail=f"Found {hook_helper.relative_to(Path.cwd())}",
        ))
    else:
        result.checks.append(Check(
            name="Hook Helper",
            status="error",
            detail="Missing tools/hooks/allow_git_push.py (required for hook preflight)",
        ))


def _check_agents(
    result: DoctorResult,
    config: Config,
    runner: CommandRunner | None,
) -> None:
    """Check agent configuration."""
    from .doctor.checks.workspace import check_agents

    checks = check_agents(config, runner)
    result.checks.extend(checks)


def _check_code_review(result: DoctorResult, config: Config) -> None:
    """Check code review configuration."""
    if not config.review_enabled:
        result.checks.append(Check(
            name="Code Review",
            status="info",
            detail="Disabled",
        ))
        return

    if not config.code_review_agent:
        result.checks.append(Check(
            name="Code Review",
            status="error",
            detail="Enabled but no default reviewer set",
        ))
        return

    if config.code_review_agent not in config.agents:
        result.checks.append(Check(
            name="Code Review",
            status="error",
            detail=f"Default reviewer '{config.code_review_agent}' not in agents",
        ))
        return

    # Check for per-agent reviewers
    per_agent = [
        (name, a.reviewer)
        for name, a in config.agents.items()
        if a.reviewer
    ]
    if per_agent:
        invalid = [f"{n}→{r}" for n, r in per_agent if r not in config.agents]
        if invalid:
            result.checks.append(Check(
                name="Code Review",
                status="error",
                detail=f"Invalid per-agent reviewers: {', '.join(invalid)}",
            ))
        else:
            result.checks.append(Check(
                name="Code Review",
                status="ok",
                detail=f"Enabled, default: {config.code_review_agent}, {len(per_agent)} per-agent",
            ))
    else:
        result.checks.append(Check(
            name="Code Review",
            status="ok",
            detail=f"Enabled, default: {config.code_review_agent}",
        ))


def _check_e2e_runner(result: DoctorResult, config: Config) -> None:
    """Check E2E test runner configuration."""
    if not config.e2e.enabled:
        result.checks.append(Check(
            name="E2E Runner",
            status="info",
            detail="Disabled",
        ))
        return

    repo_root = Path.cwd()
    e2e_checks = []
    has_error = False

    # Check pytest_args point to valid test directory
    if config.e2e.pytest_args:
        test_path = config.e2e.pytest_args[0]  # First arg is typically the path
        test_dir = repo_root / test_path
        if test_dir.exists():
            e2e_checks.append(f"tests: {test_path}")
        else:
            result.checks.append(Check(
                name="E2E Runner",
                status="warning",
                detail=f"Test path '{test_path}' not found",
            ))
            has_error = True

    # Check quarantine file
    quarantine_path = repo_root / config.e2e.quarantine_file
    if quarantine_path.exists():
        try:
            lines = [l.strip() for l in quarantine_path.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
            e2e_checks.append(f"quarantine: {len(lines)} tests")
        except Exception:
            e2e_checks.append("quarantine: unreadable")
    else:
        e2e_checks.append("quarantine: none")

    # Check DB directory is writable
    db_dir = repo_root / ".issue-orchestrator"
    if db_dir.exists() and os.access(db_dir, os.W_OK):
        e2e_checks.append("db: writable")
    elif not db_dir.exists():
        e2e_checks.append("db: will create")
    else:
        result.checks.append(Check(
            name="E2E Runner",
            status="error",
            detail=".issue-orchestrator not writable",
        ))
        has_error = True

    # Add summary if no errors
    if not has_error:
        auto = f"auto={config.e2e.auto_run_interval_minutes}m" if config.e2e.auto_run_interval_minutes > 0 else "manual"
        retry = "retry=on" if config.e2e.allow_retry_once else "retry=off"
        flake = f"flake={config.e2e.flake_threshold}/{config.e2e.flake_window_runs}runs"
        result.checks.append(Check(
            name="E2E Runner",
            status="ok",
            detail=f"Enabled ({auto}, {retry}, {flake}, {', '.join(e2e_checks)})",
        ))


def _check_review_exchange(
    result: DoctorResult,
    config: Config,
    runner: Optional[CommandRunner],
) -> None:
    """Probe MCP review exchange for configured pairs."""
    from .review_exchange_probe import probe_review_exchange

    result.checks.extend(probe_review_exchange(config, runner))

def _backup_detail_for_status(status: BackupStatus, now: float) -> dict[str, str]:
    if status.reason == "missing":
        return {"status": "missing", "detail": "db file not found"}
    if status.reason == "not enabled":
        return {"status": "skipped", "detail": "not enabled"}
    if status.reason == "disabled":
        return {"status": "disabled", "detail": "backups disabled"}
    if status.reason == "retention=0":
        return {"status": "disabled", "detail": "retention set to 0"}
    if status.reason == "error":
        detail = status.detail or "last backup failed"
        return {"status": "error", "detail": detail}
    if status.latest_mtime is None:
        return {"status": "overdue", "detail": "no backups yet"}
    age = _format_age_hours(now - status.latest_mtime)
    label = "overdue" if status.due else "ok"
    return {"status": label, "detail": f"last backup {age} ago"}


def _summarize_backup_statuses(statuses: list[BackupStatus]) -> tuple[str, str]:
    errors = [s for s in statuses if s.reason == "error"]
    overdue = [s for s in statuses if s.due and s.reason in {"none", "cadence"}]
    missing = [s for s in statuses if s.reason == "missing"]

    if errors:
        return "warning", f"{len(errors)} DB(s) failed last backup. See per-DB details."
    if overdue:
        detail = (
            f"{len(overdue)} DB(s) overdue. "
            "Suggestion: keep the orchestrator running or restart it to force a backup; "
            "adjust sqlite_backup.cadence_hours if needed."
        )
        return "warning", detail
    if missing:
        return "info", "Some DBs missing (will create on use). Backups will start after first write."
    return "ok", "Backups are up to date"


def _check_sqlite_databases(result: DoctorResult, config: Config) -> None:
    """Validate SQLite databases with quick_check when present."""
    for db in list_sqlite_databases(config):
        path = db.path_fn(config)
        if not path.exists():
            status = "info"
            detail = "Missing (will create)" if db.enabled_fn(config) else "Missing (not in use)"
            result.checks.append(Check(name=f"SQLite: {db.label}", status=status, detail=detail))
            continue

        ok, detail = quick_check_db(path)
        result.checks.append(Check(
            name=f"SQLite: {db.label}",
            status="ok" if ok else "error",
            detail=detail,
        ))


def _format_age_hours(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"{int(seconds / 60)}m"
    return f"{hours:.1f}h"


def _check_sqlite_backups(result: DoctorResult, config: Config) -> None:
    """Surface backup status and suggestions for local SQLite DBs."""
    statuses = get_backup_statuses(config)
    if not statuses:
        return

    if not config.sqlite_backup.enabled:
        result.checks.append(Check(
            name="SQLite Backups",
            status="info",
            detail="Disabled (set sqlite_backup.enabled: true to protect local state)",
        ))
        return

    now = time.time()
    per_db = {status.db.label: _backup_detail_for_status(status, now) for status in statuses}
    status, detail = _summarize_backup_statuses(statuses)

    result.checks.append(Check(
        name="SQLite Backups",
        status=status,
        detail=detail,
        expandable={
            "per_db": per_db,
        },
    ))


def _check_guardrails(
    result: DoctorResult,
    config: Config,
    runner: Optional[CommandRunner],
) -> None:
    """Run guardrail checks via test worktree."""
    if not config.repo:
        return

    if not runner:
        result.checks.append(Check(
            name="Guardrails",
            status="info",
            detail="Skipped (no CommandRunner provided)",
        ))
        return

    repo_root = Path.cwd()
    worktree_base = str(config.worktree_base) if config.worktree_base else "../"

    guardrail_checks = _check_guardrails_in_worktree(
        repo_root,
        runner,
        worktree_base,
        base_branch=config.worktree_base_branch_override,
    )
    result.checks.extend(guardrail_checks)
