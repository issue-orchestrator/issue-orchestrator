"""Run provider command with retry/circuit reporting."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from issue_orchestrator.agent_runner import AgentRunner, RunSpec, RetryPolicy
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.infra.provider_resilience import ProviderStatus, now_iso, write_provider_status
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from issue_orchestrator.ports.session_log import detect_ai_system_from_command
from issue_orchestrator.execution.session_output_adapter import SESSION_LOG_NAME


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


def _summarize_error(stdout: str, stderr: str) -> str | None:
    text = (stderr or stdout or "").strip()
    if not text:
        return None
    if len(text) > 300:
        return text[:300] + "..."
    return text


def _ensure_run_scoped_session_log(run_dir: Path) -> None:
    """Prepare provider-runner log paths without clobbering live session logs."""
    provider_stdout = run_dir / "provider-runner" / "stdout.log"
    session_log = run_dir / SESSION_LOG_NAME
    session_log.parent.mkdir(parents=True, exist_ok=True)
    provider_stdout.parent.mkdir(parents=True, exist_ok=True)

    if session_log.is_symlink():
        try:
            if session_log.resolve() == provider_stdout.resolve():
                return
        except OSError as exc:
            raise RuntimeError(
                f"failed to validate ui-session.log symlink under run_dir={run_dir}: {exc}"
            ) from exc
        session_log.unlink()
        session_log.symlink_to(provider_stdout)
        return
    if session_log.exists():
        # start_run pre-creates an empty placeholder. Replace only that case
        # so run-scoped UI log points at the provider stream path.
        if session_log.stat().st_size == 0:
            session_log.unlink()
            session_log.symlink_to(provider_stdout)
            return
        # Preserve non-empty logs to avoid clobbering already-streaming sessions.
        return
    session_log.symlink_to(provider_stdout)


def _scrub_orchestrator_env() -> None:
    """Remove orchestrator env vars so agent subprocesses (e.g. make validate-quick) see a clean env.

    Without this, env vars like ORCHESTRATOR_WORKTREE_BASE_BRANCH leak into
    the agent's shell and cause spurious unit test failures.
    """
    _strip_prefixes = ("ORCHESTRATOR_", "E2E_")
    # Keep ISSUE_ORCHESTRATOR_* vars - those are needed by agent-done
    for key in list(os.environ):
        if any(key.startswith(p) for p in _strip_prefixes):
            del os.environ[key]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    run_dir = _resolve_run_dir(args)
    output_dir = run_dir / "provider-runner"
    _ensure_run_scoped_session_log(run_dir)
    _scrub_orchestrator_env()

    provider = args.provider or detect_ai_system_from_command(args.command) or None
    retry_policy = RetryPolicy(
        max_attempts=args.max_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
        jitter=args.jitter,
    )

    runner = AgentRunner()
    result = runner.run(RunSpec(
        command=_build_command(args.command),
        working_dir=Path.cwd(),
        timeout_seconds=args.timeout_seconds,
        output_dir=output_dir,
        retry_policy=retry_policy,
        use_pty=True,
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
        last_error_summary=_summarize_error(result.stdout, result.stderr),
        last_attempt_at=now_iso(),
    )
    write_provider_status(run_dir, status)

    if result.exit_code is not None:
        return int(result.exit_code)
    return 124 if result.timed_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
