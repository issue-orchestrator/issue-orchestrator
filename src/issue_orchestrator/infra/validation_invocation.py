"""Validation invocation helpers.

Builds validation command, environment, and stdin context based on config.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config, resolve_relative_path


class ValidationInvocationError(RuntimeError):
    """Raised when validation configuration is invalid."""


@dataclass(frozen=True)
class ValidationInvocation:
    """Resolved validation command invocation."""

    command: str | list[str]
    command_display: str
    env: dict[str, str]
    input_text: Optional[str]
    timeout_seconds: int


@dataclass(frozen=True)
class ValidationSpec:
    """Resolved validation spec after applying per-agent overrides."""

    script: Optional[str]
    args: list[str]
    env: dict[str, str]
    cmd: Optional[str]
    timeout_seconds: int


class ValidationResolver:
    """Resolve validation commands and stdin context for a run."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def resolve(
        self,
        *,
        worktree: Path,
        run_dir: Path,
        agent_label: Optional[str],
        mode: str,
    ) -> Optional[ValidationInvocation]:
        spec = _resolve_validation_spec(self._config, agent_label)
        if spec is None:
            return None

        if spec.script:
            script_path = resolve_relative_path(spec.script, worktree)
            _ensure_executable(script_path)
            command_list = [str(script_path), *spec.args]
            env = _merge_env(spec.env)
            context = _build_context(
                config=self._config,
                worktree=worktree,
                run_dir=run_dir,
                agent_label=agent_label,
                mode=mode,
                validation_spec=spec,
            )
            return ValidationInvocation(
                command=command_list,
                command_display=shlex.join(command_list),
                env=env,
                input_text=json.dumps(context, sort_keys=True),
                timeout_seconds=spec.timeout_seconds,
            )

        if spec.cmd:
            if spec.args:
                raise ValidationInvocationError(
                    "validation.args is only supported when validation.script is set"
                )
            env = _merge_env(spec.env)
            return ValidationInvocation(
                command=spec.cmd,
                command_display=spec.cmd,
                env=env,
                input_text=None,
                timeout_seconds=spec.timeout_seconds,
            )

        return None


def _resolve_validation_spec(
    config: Config,
    agent_label: Optional[str],
) -> Optional[ValidationSpec]:
    base = config.validation
    if not (base.script or base.cmd):
        return None

    override = None
    if agent_label and agent_label in config.agents:
        override = config.agents[agent_label].validation

    script = base.script
    args = list(base.args)
    env = dict(base.env)
    cmd = base.cmd
    timeout_seconds = base.timeout_seconds

    if override:
        if override.script is not None:
            script = override.script
        if override.args is not None:
            args = list(override.args)
        if override.env is not None:
            env.update(override.env)

    return ValidationSpec(
        script=script,
        args=args,
        env=env,
        cmd=cmd,
        timeout_seconds=timeout_seconds,
    )


def _merge_env(extra_env: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(extra_env)
    return env


def _build_context(
    *,
    config: Config,
    worktree: Path,
    run_dir: Path,
    agent_label: Optional[str],
    mode: str,
    validation_spec: ValidationSpec,
) -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "agent_label": agent_label,
        "repo_root": str(worktree),
        "run_dir": str(run_dir),
        "config": config.to_dict(),
        "validation": {
            "script": validation_spec.script,
            "args": list(validation_spec.args),
            "env": dict(validation_spec.env),
            "cmd": validation_spec.cmd,
            "timeout_seconds": validation_spec.timeout_seconds,
        },
    }


def _ensure_executable(script_path: Path) -> None:
    if not script_path.exists():
        raise ValidationInvocationError(f"validation.script not found: {script_path}")
    if not os.access(script_path, os.X_OK):
        raise ValidationInvocationError(
            f"validation.script is not executable: {script_path}"
        )
