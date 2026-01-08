"""OrchestratorProcess for e2e tests.

Manages the orchestrator subprocess lifecycle: start, stop, log capture.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .inflight_tracker import trigger_refresh

if TYPE_CHECKING:
    from issue_orchestrator.infra.config import Config

logger = logging.getLogger(__name__)

# Suppress httpx INFO logs (pollutes output with API health checks)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

E2E_LOG_DIR = Path("/tmp/e2e-orchestrator-logs")
E2E_LOG_DIR.mkdir(exist_ok=True)


def _keep_artifacts() -> bool:
    """Return True if e2e cleanup should be skipped."""
    return os.environ.get("E2E_KEEP_ARTIFACTS") == "1"


def _keep_remote_artifacts() -> bool:
    """Return True if remote cleanup (PRs/branches/issues) should be skipped."""
    return os.environ.get("E2E_KEEP_REMOTE_ARTIFACTS") == "1"


class OrchestratorProcess:
    """Wrapper for orchestrator subprocess with IPC support."""

    def __init__(self, config: "Config", project_root: Path, tmux_session: str = "orchestrator"):
        self.config = config
        self.project_root = project_root
        self.tmux_session = tmux_session
        self.process: subprocess.Popen | None = None
        self.ipc_socket_path: Path | None = None
        self._output_lines: list[str] = []
        self._log_thread: threading.Thread | None = None
        self._stop_logging = False
        self._log_file: Path | None = None
        self._orchestrator_log_file: Path | None = None
        self._log_handle: "open | None" = None
        self._config_path: Path | None = None
        self._last_log_time: float | None = None

    def _write_e2e_config(self) -> Path:
        """Write an ephemeral config file so the CLI uses the e2e config."""
        config_dir = Path("/tmp/e2e-orchestrator-configs")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"issue-orchestrator.e2e.{os.getpid()}.yaml"
        data = {
            "repo": self.config.repo,
            "repo_root": str(self.config.repo_root),
            "worktree_base": str(self.config.worktree_base),
            "filter_label": self.config.filter_label,
            "github_token_env": self.config.github_token_env,
            "ui_mode": self.config.ui_mode,
            "web_port": self.config.web_port,
            "control_api_port": self.config.control_api_port,
            "queue_refresh_seconds": self.config.queue_refresh_seconds,
            "e2e_pr_labels": self.config.e2e_pr_labels,
            "gh_write_verify_timeout_seconds": self.config.gh_write_verify_timeout_seconds,
            "gh_write_verify_initial_delay_ms": self.config.gh_write_verify_initial_delay_ms,
            "gh_write_verify_max_delay_ms": self.config.gh_write_verify_max_delay_ms,
            "gh_write_verify_backoff": self.config.gh_write_verify_backoff,
            "gh_write_verify_jitter_ms": self.config.gh_write_verify_jitter_ms,
            "gh_audit_enabled": self.config.gh_audit_enabled,
            "gh_audit_events": self.config.gh_audit_events,
            "gh_audit_file": self.config.gh_audit_file,
            "concurrency": {
                "max_concurrent_sessions": self.config.max_concurrent_sessions,
                "session_timeout_minutes": self.config.session_timeout_minutes,
            },
            "agents": {
                label: {
                    "prompt": str(cfg.prompt_path),
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "permission_mode": cfg.permission_mode,
                    "command": cfg.command,
                    "meta_agent": cfg.meta_agent,
                }
                for label, cfg in self.config.agents.items()
            },
            "validation": {
                "agent_gate": {
                    "cmd": self.config.validation.agent_gate.cmd,
                    "timeout_seconds": self.config.validation.agent_gate.timeout_seconds,
                },
                "publish_gate": {
                    "cmd": self.config.validation.publish_gate.cmd,
                    "timeout_seconds": self.config.validation.publish_gate.timeout_seconds,
                },
            },
            "validation_policy": {
                "publish_requires": self.config.validation_policy.publish_requires,
                "agent_runs": self.config.validation_policy.agent_runs,
            },
            "review": {
                "code_review_agent": self.config.code_review_agent,
                "code_review_label": self.config.code_review_label,
                "code_reviewed_label": self.config.code_reviewed_label,
                "max_rework_cycles": self.config.max_rework_cycles,
                "triage_review_agent": self.config.triage_review_agent,
                "triage_review_label": self.config.triage_review_label,
                "triage_reviewed_label": self.config.triage_reviewed_label,
                "triage_review_threshold": self.config.triage_review_threshold,
                "triage_review_on_failure": self.config.triage_review_on_failure,
            },
            "cleanup": {
                "with_triage": {
                    "close_ai_session_tabs": self.config.cleanup.with_triage.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.with_triage.remove_worktrees,
                },
                "without_triage": {
                    "wait_for_code_review": self.config.cleanup.without_triage.wait_for_code_review,
                    "close_ai_session_tabs": self.config.cleanup.without_triage.close_ai_session_tabs,
                    "remove_worktrees": self.config.cleanup.without_triage.remove_worktrees,
                },
            },
            "dangerous": {
                "allow_unsupported_agents": self.config.dangerous.allow_unsupported_agents,
            },
        }
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))
        self._config_path = config_path
        return config_path

    def _log_reader(self) -> None:
        """Background thread to read and print orchestrator output."""
        import select
        import sys

        if self.process is None:
            return

        while not self._stop_logging and self.process.poll() is None:
            # Use select to check for available output
            if self.process.stderr:
                readable, _, _ = select.select([self.process.stderr], [], [], 0.5)
                if readable:
                    try:
                        line = self.process.stderr.readline()
                    except ValueError:
                        break
                    if line:
                        text = line.decode('utf-8', errors='replace').rstrip()
                        self._output_lines.append(text)
                        self._last_log_time = time.time()
                        # Always write to persistent log file
                        if self._log_handle:
                            self._log_handle.write(f"{text}\n")
                            self._log_handle.flush()
                        # Print orchestrator events with prefix (filtered for readability)
                        if any(kw in text for kw in ['[EVENT]', 'Session', 'Issue', 'PR', 'Review', 'launch', 'complet', 'start', 'ERROR', 'WARN', 'failed', 'timeout']):
                            print(f"  [ORCH] {text}", file=sys.stderr, flush=True)

    def start(self, max_issues: int = 1, extra_args: list[str] | None = None) -> None:
        """Start the orchestrator process."""
        import sys
        from datetime import datetime

        # Create persistent log file (survives Ctrl+C)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_file = E2E_LOG_DIR / f"e2e-{timestamp}.log"
        orchestrator_log_file = E2E_LOG_DIR / f"orchestrator-{timestamp}.log"
        self._orchestrator_log_file = orchestrator_log_file
        self._log_handle = open(self._log_file, "w")

        # Clean up old log files (keep last 10)
        log_files = sorted(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_log in log_files[10:]:
            old_log.unlink()

        # Print debug paths upfront for troubleshooting
        worktree_dir = Path("/tmp/e2e-worktrees")  # E2E worktree location
        claude_logs = Path.home() / ".claude" / "logs"
        print(f"\n  {'='*60}", flush=True)
        print(f"  [E2E DEBUG PATHS]", flush=True)
        print(f"    Orchestrator log: {self._log_file}", flush=True)
        print(f"    Orchestrator file: {orchestrator_log_file}", flush=True)
        print(f"    Worktrees:        {worktree_dir}", flush=True)
        print(f"    Claude logs:      {claude_logs}", flush=True)
        print(f"    Keep artifacts:   {_keep_artifacts()}", flush=True)
        print(f"    Keep remote:      {_keep_remote_artifacts()}", flush=True)
        if os.environ.get("E2E_CLAUDE_ARGS"):
            print(f"    E2E_CLAUDE_ARGS:  {os.environ.get('E2E_CLAUDE_ARGS')}", flush=True)
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            print(f"    E2E_PROMPT_MODE:  {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}", flush=True)
        print(f"  {'='*60}\n", flush=True)

        # Write header to log file
        self._log_handle.write(f"E2E Test Run: {timestamp}\n")
        self._log_handle.write(f"Orchestrator file: {orchestrator_log_file}\n")
        self._log_handle.write(f"Worktrees: {worktree_dir}\n")
        self._log_handle.write(f"Claude logs: {claude_logs}\n")
        self._log_handle.write(f"Keep artifacts: {_keep_artifacts()}\n")
        self._log_handle.write(f"Keep remote: {_keep_remote_artifacts()}\n")
        self._log_handle.write(f"E2E_KEEP_ARTIFACTS: {os.environ.get('E2E_KEEP_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_KEEP_REMOTE_ARTIFACTS: {os.environ.get('E2E_KEEP_REMOTE_ARTIFACTS', '')}\n")
        self._log_handle.write(f"E2E_CONTROL_API_PORT: {os.environ.get('E2E_CONTROL_API_PORT', '')}\n")
        if os.environ.get("E2E_CLAUDE_ARGS"):
            self._log_handle.write(f"E2E_CLAUDE_ARGS: {os.environ.get('E2E_CLAUDE_ARGS')}\n")
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            self._log_handle.write(f"E2E_CLAUDE_PROMPT_MODE: {os.environ.get('E2E_CLAUDE_PROMPT_MODE')}\n")
        self._log_handle.write("=" * 60 + "\n\n")
        self._log_handle.flush()

        # Prefer project .venv (has e2e deps like fastapi); fall back to pytest venv
        preferred_bin = self.project_root / ".venv" / "bin" / "issue-orchestrator"
        venv_bin = preferred_bin if preferred_bin.exists() else Path(sys.executable).parent / "issue-orchestrator"

        # Allow UI mode override via env var for interactive debugging
        ui_mode = os.environ.get("E2E_UI_MODE", "tmux")

        config_path = self._write_e2e_config()
        cmd = [
            str(venv_bin), "--config", str(config_path), "start",
            "--label", "test-data",
            "--max-issues", str(max_issues),
            "--ui-mode", ui_mode,
        ]

        # Add dashboard flags based on UI mode
        if ui_mode == "web":
            cmd.extend(["--port", os.environ.get("E2E_WEB_PORT", "8080")])
            print(f"  [E2E] Web UI available at http://localhost:{os.environ.get('E2E_WEB_PORT', '8080')}", flush=True)
        else:
            cmd.append("--no-dashboard")  # Don't start TUI in tests

        if extra_args:
            cmd.extend(extra_args)

        # Set up environment with fast publish_gate for e2e tests
        env = os.environ.copy()
        env["ORCHESTRATOR_PUBLISH_GATE_CMD"] = "echo 'e2e publish gate validation'"
        env["ORCHESTRATOR_PUBLISH_GATE_TIMEOUT"] = "30"
        env["ORCHESTRATOR_LOG_LEVEL"] = "DEBUG"
        env["ORCHESTRATOR_LOG_FILE"] = str(orchestrator_log_file)
        env["PYTHONUNBUFFERED"] = "1"
        env["ORCHESTRATOR_LOG_TO_STDERR"] = "1"
        # Set tmux session name for e2e test isolation
        env["ORCHESTRATOR_TMUX_SESSION"] = self.tmux_session
        if os.environ.get("E2E_CLAUDE_ARGS"):
            env["ORCHESTRATOR_CLAUDE_ARGS"] = os.environ["E2E_CLAUDE_ARGS"]
        if os.environ.get("E2E_CLAUDE_PROMPT_MODE"):
            env["ORCHESTRATOR_CLAUDE_PROMPT_MODE"] = os.environ["E2E_CLAUDE_PROMPT_MODE"]
        env["ORCHESTRATOR_WORKTREE_PER_SESSION"] = os.environ.get("E2E_WORKTREE_PER_SESSION", "1")
        env["ORCHESTRATOR_DISABLE_WORKTREE_REUSE"] = os.environ.get("E2E_DISABLE_WORKTREE_REUSE", "1")
        # Explicitly pass dry-run mode for e2e tests (skips git push and PR creation)
        if os.environ.get("E2E_DRY_RUN_PUSH"):
            env["E2E_DRY_RUN_PUSH"] = os.environ["E2E_DRY_RUN_PUSH"]
            print(f"  [E2E] E2E_DRY_RUN_PUSH={env['E2E_DRY_RUN_PUSH']}", flush=True)

        print(f"  [E2E] Starting orchestrator: {' '.join(cmd)}", flush=True)
        self._log_handle.write(f"Command: {' '.join(cmd)}\n\n")
        self._log_handle.flush()

        self.process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Start background log reader
        self._stop_logging = False
        self._log_thread = threading.Thread(target=self._log_reader, daemon=True)
        self._log_thread.start()

        # Give it time to start
        time.sleep(3)
        print(f"  [E2E] Orchestrator started (pid={self.process.pid})", flush=True)

    def stop(self) -> tuple[str, str]:
        """Stop the orchestrator and return stdout/stderr."""
        if self.process is None:
            return "", ""

        print(f"  [E2E] Stopping orchestrator (pid={self.process.pid})...", flush=True)

        # Stop the log reader thread
        self._stop_logging = True

        # Send SIGTERM for graceful shutdown
        self.process.send_signal(signal.SIGTERM)

        try:
            stdout, stderr = self.process.communicate(timeout=5)
            self._cleanup_tmux_sessions()
            self._cleanup_log_tailers()
            self._close_log_file()
            print(f"  [E2E] Orchestrator stopped gracefully", flush=True)
            return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
        except subprocess.TimeoutExpired:
            print(f"  [E2E] Sending second SIGTERM...", flush=True)
            self.process.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = self.process.communicate(timeout=5)
                self._cleanup_tmux_sessions()
                self._cleanup_log_tailers()
                self._close_log_file()
                print(f"  [E2E] Orchestrator stopped after second SIGTERM", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
            except subprocess.TimeoutExpired:
                print(f"  [E2E] Force killing orchestrator...", flush=True)
                self.process.kill()
                stdout, stderr = self.process.communicate()
                self._cleanup_tmux_sessions()
                self._cleanup_log_tailers()
                self._close_log_file()
                print(f"  [E2E] Orchestrator killed", flush=True)
                return stdout.decode() if stdout else "", stderr.decode() if stderr else ""

    @property
    def log_path(self) -> Path | None:
        """Get the path to the persistent log file."""
        return self._log_file

    def orchestrator_log_path(self) -> Path | None:
        """Get the path to the orchestrator log file."""
        return self._orchestrator_log_file

    def last_log_age_seconds(self) -> float:
        """Return seconds since last orchestrator stderr log line."""
        if not self._last_log_time:
            return 0.0
        return time.time() - self._last_log_time

    def _close_log_file(self) -> None:
        """Close log file and print location for debugging."""
        if self._log_handle:
            self._log_handle.write(f"\n{'='*60}\nOrchestrator stopped at {time.strftime('%H:%M:%S')}\n")
            self._log_handle.close()
            self._log_handle = None
        if self._log_file:
            print(f"  [E2E] Full log saved to: {self._log_file}", flush=True)
        if self._config_path and self._config_path.exists():
            try:
                self._config_path.unlink()
            except OSError:
                pass

    def _cleanup_tmux_sessions(self) -> None:
        """Clean up any tmux windows created by e2e tests."""
        if _keep_artifacts():
            return
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-t", self.tmux_session, "-F", "#{window_index}:#{window_name}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if "E2E-TEST" in line or "E2E-" in line:
                    window_index = line.split(":")[0]
                    subprocess.run(
                        ["tmux", "kill-window", "-t", f"{self.tmux_session}:{window_index}"],
                        capture_output=True,
                    )
        except Exception:
            pass

    def _cleanup_log_tailers(self) -> None:
        """Stop lingering session.log tail processes from tmux pipe-pane."""
        if _keep_artifacts():
            return
        try:
            result = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return
        for line in result.stdout.splitlines():
            if "cat >>" not in line or ".issue-orchestrator/session.log" not in line:
                continue
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue

    def is_running(self) -> bool:
        """Check if orchestrator is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def _check_api_running(self) -> bool:
        """Check if the orchestrator API is responding."""
        import httpx
        api_port = self.config.control_api_port
        if not api_port:
            # No API port configured, assume running if we got this far
            return True
        try:
            resp = httpx.get(f"http://127.0.0.1:{api_port}/api/status", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def request_refresh(self) -> bool:
        """Request the orchestrator to refresh issues on next tick via control API."""
        return trigger_refresh()
