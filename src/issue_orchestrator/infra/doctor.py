"""Unified diagnostics for issue-orchestrator.

This module provides a single doctor function that both CLI and web can call.
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Config


@dataclass
class Check:
    """A single diagnostic check result."""
    name: str
    status: str  # "ok", "warning", "error", "info"
    detail: str


@dataclass
class DoctorResult:
    """Result of running diagnostics."""
    checks: list[Check] = field(default_factory=list)

    @property
    def overall(self) -> str:
        """Overall status based on all checks."""
        if any(c.status == "error" for c in self.checks):
            return "error"
        if any(c.status == "warning" for c in self.checks):
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "overall": self.overall,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


def run_doctor(config: Optional[Config] = None, config_path: Optional[Path] = None) -> DoctorResult:
    """Run all diagnostic checks.

    Args:
        config: Optional pre-loaded config (used by web when orchestrator is running)
        config_path: Optional path to config file (used by CLI)

    Returns:
        DoctorResult with all check results
    """
    from ..adapters.github.http_client import (
        _read_keyring_token,
        validate_github_token,
        KEYRING_SERVICE,
        KEYRING_USERNAME,
    )

    result = DoctorResult()

    # === GitHub Authentication ===
    env_vars = ["ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]
    token_sources = []
    for var in env_vars:
        value = os.environ.get(var)
        if value:
            masked = value[:4] + "..." + value[-4:] if len(value) > 12 else "***"
            token_sources.append(f"{var}: {masked}")

    keyring_token = _read_keyring_token()
    if keyring_token:
        masked = keyring_token[:4] + "..." + keyring_token[-4:] if len(keyring_token) > 12 else "***"
        token_sources.append(f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME}): {masked}")

    if token_sources:
        result.checks.append(Check(
            name="Token Sources",
            status="ok",
            detail=", ".join(token_sources),
        ))
    else:
        result.checks.append(Check(
            name="Token Sources",
            status="error",
            detail="No GitHub token found",
        ))

    # Validate token with GitHub
    token_result = validate_github_token()
    if token_result.valid:
        result.checks.append(Check(
            name="GitHub Auth",
            status="ok",
            detail=f"Authenticated as: {token_result.username}",
        ))
    else:
        result.checks.append(Check(
            name="GitHub Auth",
            status="error",
            detail=token_result.error or "Unknown error",
        ))

    # === Configuration ===
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
                return result
        else:
            result.checks.append(Check(
                name="Config File",
                status="warning",
                detail="Not found",
            ))
            return result

    if config is None:
        # Try to find config in current directory
        for name in [".issue-orchestrator.yaml", ".issue-orchestrator.yml"]:
            if Path(name).exists():
                try:
                    config = Config.load(Path(name))
                    result.checks.append(Check(
                        name="Config File",
                        status="ok",
                        detail=name,
                    ))
                    break
                except Exception as e:
                    result.checks.append(Check(
                        name="Config File",
                        status="error",
                        detail=f"Failed to load {name}: {e}",
                    ))
                    return result
        else:
            result.checks.append(Check(
                name="Config File",
                status="warning",
                detail="Not found in current directory",
            ))
            return result

    # Validate config
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

    # Repository
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

    # Agents
    agent_count = len(config.agents)
    if agent_count > 0:
        result.checks.append(Check(
            name="Agents",
            status="ok",
            detail=f"{agent_count} configured",
        ))

        # Check agent scripts
        missing_scripts = []
        for name, agent_cfg in config.agents.items():
            cmd_parts = agent_cfg.command.split()
            if cmd_parts:
                script = cmd_parts[0]
                if not shutil.which(script) and not Path(script).exists():
                    missing_scripts.append(f"{name}: {script}")

        if missing_scripts:
            result.checks.append(Check(
                name="Agent Scripts",
                status="error",
                detail=f"Missing: {', '.join(missing_scripts[:3])}" + ("..." if len(missing_scripts) > 3 else ""),
            ))
        else:
            result.checks.append(Check(
                name="Agent Scripts",
                status="ok",
                detail="All found",
            ))
    else:
        result.checks.append(Check(
            name="Agents",
            status="warning",
            detail="None configured",
        ))

    # === Code Review ===
    if config.review_enabled:
        if config.code_review_agent:
            if config.code_review_agent in config.agents:
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
            else:
                result.checks.append(Check(
                    name="Code Review",
                    status="error",
                    detail=f"Default reviewer '{config.code_review_agent}' not in agents",
                ))
        else:
            result.checks.append(Check(
                name="Code Review",
                status="error",
                detail="Enabled but no default reviewer set",
            ))
    else:
        result.checks.append(Check(
            name="Code Review",
            status="info",
            detail="Disabled",
        ))

    return result
