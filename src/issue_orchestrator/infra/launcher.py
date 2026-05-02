"""Unified launcher for the issue orchestrator.

All entry points (CLI, Control Center, MCP) converge through this module
to ensure consistent pre-flight (doctor) checks before starting.

Two launch modes:
- ``launch_preflight_only``: runs doctor checks, returns result. CLI uses
  this because it builds the orchestrator in-process afterwards.
- ``launch_subprocess``: runs doctor checks then calls ``supervisor.start()``
  to launch the orchestrator as a subprocess. CC and MCP use this.

``preflight`` is an alias for ``launch_preflight_only`` kept for readability
when the caller only wants to display readiness (e.g. CC page load).
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import supervisor
from .config import Config
from .doctor import run_doctor
from .doctor.types import DoctorResult
from .repo_lock import AlreadyRunning
from .supervisor import SupervisorOps
from ..ports.command_runner import CommandRunner

# Type alias for the doctor function, enabling DI in tests.
DoctorFn = Callable[..., DoctorResult]

logger = logging.getLogger(__name__)


@dataclass
class LaunchResult:
    """Result of a launcher operation."""

    doctor: DoctorResult
    launched: bool
    status: str  # "ok" | "doctor_error" | "doctor_warning" | "launch_error"
    error: Optional[str] = None
    supervisor: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "doctor": self.doctor.to_dict(),
            "launched": self.launched,
            "status": self.status,
        }
        if self.error is not None:
            result["error"] = self.error
        if self.supervisor is not None:
            result["supervisor"] = self.supervisor
        return result


def _run_preflight(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> tuple[DoctorResult, str]:
    """Run doctor checks and return (result, status_string).

    Args:
        doctor_fn: Callable to use instead of ``run_doctor``.
            Also skipped when ``ISSUE_ORCHESTRATOR_SKIP_DOCTOR=1`` is set
            (needed by integration tests that spawn subprocesses).

    Returns:
        (doctor_result, status) where status is "ok", "doctor_warning",
        or "doctor_error".
    """
    if os.environ.get("ISSUE_ORCHESTRATOR_SKIP_DOCTOR") == "1":
        return DoctorResult(checks=[]), "ok"
    fn = doctor_fn or run_doctor
    doctor_start = time.time()
    doctor_result = fn(config=config, runner=runner)
    logger.info(
        "[STARTUP_TIMING] phase=preflight_doctor elapsed=%.3fs overall=%s checks=%d",
        time.time() - doctor_start,
        doctor_result.overall,
        len(doctor_result.checks),
    )
    if doctor_result.overall == "error":
        return doctor_result, "doctor_error"
    if doctor_result.overall == "warning":
        return doctor_result, "doctor_warning"
    return doctor_result, "ok"


def preflight(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks only. Returns LaunchResult with launched=False.

    Use for "show readiness" — runs doctor without starting anything.
    """
    doctor_result, status = _run_preflight(config, runner, doctor_fn=doctor_fn)
    return LaunchResult(
        doctor=doctor_result,
        launched=False,
        status=status,
    )


def launch_preflight_only(
    config: Config,
    runner: Optional[CommandRunner] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks only, for CLI which builds in-process.

    CLI calls this, then proceeds to ``build_orchestrator()`` itself.
    Alias for ``preflight()`` — named differently for clarity at call sites.
    """
    return preflight(config, runner, doctor_fn=doctor_fn)


def _start_with_supervisor(
    sv: SupervisorOps,
    *,
    repo_root: Path,
    config: Config,
    config_name: str,
    instance_id: Optional[str],
    port: Optional[int],
    expected_identity: Optional[dict[str, Any]],
    start_paused: bool,
) -> dict[str, Any]:
    if config.instances > 1 and instance_id is None:
        start_instances_kwargs: dict[str, Any] = {
            "repo_root": repo_root,
            "config_name": config_name,
        }
        if expected_identity is not None:
            start_instances_kwargs["expected_identity"] = expected_identity
        if start_paused:
            start_instances_kwargs["start_paused"] = True
        infos = sv.start_instances(**start_instances_kwargs)
        return {
            "instances": [
                {"pid": info.pid, "port": info.http_port, "instance_id": info.instance_id}
                for info in infos
            ],
        }

    start_kwargs: dict[str, Any] = {
        "repo_root": repo_root,
        "config_name": config_name,
        "instance_id": instance_id,
        "port": port,
    }
    if expected_identity is not None:
        start_kwargs["expected_identity"] = expected_identity
    if start_paused:
        start_kwargs["start_paused"] = True
    info = sv.start(**start_kwargs)

    supervisor_data = {
        "pid": info.pid,
        "port": info.http_port,
    }
    if info.instance_id:
        supervisor_data["instance_id"] = info.instance_id
    return supervisor_data


def launch_subprocess(
    repo_root: Path,
    config: Config,
    config_name: str = "default.yaml",
    runner: Optional[CommandRunner] = None,
    instance_id: Optional[str] = None,
    port: Optional[int] = None,
    expected_identity: Optional[dict[str, Any]] = None,
    start_paused: bool = False,
    supervisor_ops: Optional[SupervisorOps] = None,
    doctor_fn: Optional[DoctorFn] = None,
) -> LaunchResult:
    """Run doctor checks, then supervisor.start() if checks pass.

    Used by CC and MCP entry points.

    Args:
        repo_root: Repository root path.
        config: Loaded configuration.
        config_name: Config file name for the supervisor.
        runner: Optional command runner for guardrails checks.
        instance_id: Optional instance ID for multi-instance mode.
        port: Optional port override.
        supervisor_ops: Optional supervisor operations (DI for tests).
        doctor_fn: Callable that runs doctor checks. Defaults to
            ``run_doctor``.  Tests can inject a no-op to skip checks.

    Returns:
        LaunchResult with doctor results and supervisor info.
    """
    doctor_result, status = _run_preflight(config, runner, doctor_fn=doctor_fn)

    if status == "doctor_error":
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status="doctor_error",
        )

    # Doctor passed (ok or warning) — start the orchestrator subprocess
    sv = supervisor_ops or supervisor
    try:
        supervisor_data = _start_with_supervisor(
            sv,
            repo_root=repo_root,
            config=config,
            config_name=config_name,
            instance_id=instance_id,
            port=port,
            expected_identity=expected_identity,
            start_paused=start_paused,
        )

        return LaunchResult(
            doctor=doctor_result,
            launched=True,
            status=status,  # "ok" or "doctor_warning"
            supervisor=supervisor_data,
        )
    except AlreadyRunning:
        from .repo_lock import read_lock
        info = read_lock(repo_root, instance_id)
        supervisor_data = None
        if info:
            supervisor_data = {
                "pid": info.pid,
                "port": info.http_port,
            }
            if info.instance_id:
                supervisor_data["instance_id"] = info.instance_id
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status="already_running",
            error="Orchestrator already running",
            supervisor=supervisor_data,
        )
    except Exception as exc:
        logger.exception("Failed to launch orchestrator subprocess")
        return LaunchResult(
            doctor=doctor_result,
            launched=False,
            status="launch_error",
            error=str(exc),
        )
