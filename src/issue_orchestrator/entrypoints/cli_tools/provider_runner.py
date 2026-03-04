"""Run provider command with retry/circuit reporting.

Output capture is NOT handled here. The agent's stdout/stderr are inherited
from the parent process (pexpect PTY), flowing through CleaningLogWriter
to ui-session.log. This module only handles retry logic and circuit breaker
status reporting.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner
from issue_orchestrator.execution.agent_runner_types import AgentSpec, RetryPolicy
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.infra.provider_resilience import ProviderStatus, now_iso, write_provider_status
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from issue_orchestrator.ports.session_log import detect_ai_system_from_command


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run provider command with resilience hooks")
    parser.add_argument("--command", required=True, help="Provider command to execute")
    parser.add_argument("--provider", default=None, help="Provider name (optional)")
    parser.add_argument("--timeout-seconds", type=int, default=60 * 45)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--initial-backoff-seconds", type=int, default=5)
    parser.add_argument("--max-backoff-seconds", type=int, default=60)
    parser.add_argument("--jitter", action="store_true", default=True)
    parser.add_argument("--no-jitter", action="store_false", dest="jitter")
    parser.add_argument("--run-dir", default=None, help="Session run directory (optional)")
    return parser.parse_args(argv)


def _resolve_run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir)
    env_dir = os.environ.get(f"{ENV_PREFIX}RUN_DIR")
    if env_dir:
        return Path(env_dir)
    raise RuntimeError("Missing session run directory (set ISSUE_ORCHESTRATOR_RUN_DIR or --run-dir)")


def _build_command(command: str) -> list[str]:
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-lc", command]


def _summarize_error(stderr: str) -> str | None:
    text = stderr.strip()
    if not text:
        return None
    if len(text) > 300:
        return text[:300] + "..."
    return text


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    run_dir = _resolve_run_dir(args)
    output_dir = run_dir / "provider-runner"

    provider = args.provider or detect_ai_system_from_command(args.command) or None
    retry_policy = RetryPolicy(
        max_attempts=args.max_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
        jitter=args.jitter,
    )

    runner = SubprocessAgentRunner()
    result = runner.run(AgentSpec(
        command=_build_command(args.command),
        working_dir=Path.cwd(),
        timeout_seconds=args.timeout_seconds,
        output_dir=output_dir,
        retry_policy=retry_policy,
    ))

    error_type = None
    if result.provider_error_type:
        error_type = ProviderErrorType(result.provider_error_type.value)

    status = ProviderStatus(
        provider=provider,
        error_type=error_type,
        attempts=result.attempts,
        succeeded=result.succeeded,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        last_error_summary=_summarize_error(result.stderr),
        last_attempt_at=now_iso(),
    )
    write_provider_status(run_dir, status)

    if result.exit_code is not None:
        return int(result.exit_code)
    return 124 if result.timed_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
