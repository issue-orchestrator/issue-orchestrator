"""via-local-loop review exchange with persistent PTY sessions.

Each agent (coder, reviewer) runs as a persistent PTY session that stays
alive across review rounds. Communication uses file-based prompts sent
via stdin references; completion is detected via structured files written
by coding-done / reviewer-done.

This is a discrete abstraction for the via-local-loop exchange mode,
separate from other modes (via-draft-pr, via-mcp).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pexpect

from ..agent_runner import get_provider
from ..agent_runner.env_filter import build_filtered_env
from ..domain.models import AgentConfig
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import get_repo_log_path
from ..infra.terminal_recording import MirroredTerminalRecordingWriter
from ..ports import EventSink, make_trace_event
from ..ports.session_output import SessionOutput
from ..events import EventName, EventContext
from ..resources import get_reviewer_done_instructions

from ..control.review_exchange_loop import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
    _escape_claude_project_path,
    _seed_validation_record,
    _validation_passed,
    _write_round_log,
    _write_summary,
)
from ..control.isolation import build_runtime_tool_env

logger = logging.getLogger(__name__)

_COMPLETION_POLL_INTERVAL = 2.0  # seconds between completion file checks
_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40


# ---------------------------------------------------------------------------
# Pre-flight cleanup
# ---------------------------------------------------------------------------


def _pid_cwd_in_worktree(pid: int, worktree_str: str) -> bool:
    """Return True if *pid*'s cwd is inside *worktree_str*."""
    try:
        lsof = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    cwd_found = False
    for line in lsof.stdout.splitlines():
        if line == "fcwd":
            cwd_found = True
        elif cwd_found and line.startswith("n"):
            proc_cwd = line[1:]
            return proc_cwd == worktree_str or proc_cwd.startswith(worktree_str + "/")
        else:
            cwd_found = False
    return False


def _wait_and_force_kill(pids: list[int], timeout: float) -> None:
    """Wait for *pids* to exit; SIGKILL any that survive past *timeout*."""
    deadline = time.monotonic() + timeout
    for pid in pids:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _kill_existing_claude_sessions(worktree_path: Path, *, timeout: float = 10.0) -> None:
    """Kill any existing Claude Code processes whose cwd is inside worktree_path.

    Claude Code uses project-level locking so a second session in the same
    project directory will hang during initialization.  The coder session
    from the coding phase may still be alive when the review exchange starts;
    we must terminate it first.
    """
    worktree_str = str(worktree_path)
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return
    if result.returncode != 0 or not result.stdout.strip():
        return

    pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    killed: list[int] = []
    for pid in pids:
        try:
            if _pid_cwd_in_worktree(pid, worktree_str):
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except (OSError, ProcessLookupError):
            continue

    if killed:
        logger.info(
            "Terminated %d existing Claude session(s) in %s: pids=%s",
            len(killed), worktree_path, killed,
        )
        _wait_and_force_kill(killed, timeout)


# ---------------------------------------------------------------------------
# Persistent PTY session
# ---------------------------------------------------------------------------


@dataclass
class _PtySession:
    """A persistent PTY session for one agent."""

    role: str
    child: pexpect.spawn[str]
    log_file: Any  # terminal replay writer
    log_path: Path
    completion_path: Path  # absolute path to completion file

    def send_follow_up(self, prompt_file: Path) -> None:
        """Send a follow-up prompt by referencing a file.

        Uses PTY stdin to deliver the prompt. Claude Code's interactive TUI
        accepts follow-up messages via sendline().
        """
        msg = f"Read and follow your next instructions in {prompt_file}"
        self.child.sendline(msg)

    @property
    def alive(self) -> bool:
        return self.child.isalive()

    def terminate(self, timeout: float = 30.0) -> None:
        """Gracefully terminate the session."""
        if not self.child.isalive():
            self._close_log()
            return
        try:
            self.child.send("/exit\r")
            self.child.expect(pexpect.EOF, timeout=timeout)
        except (pexpect.TIMEOUT, pexpect.ExceptionPexpect):
            try:
                self.child.terminate(force=True)
            except Exception:
                pass
        finally:
            try:
                self.child.close()
            except Exception:
                pass
            self._close_log()

    def _close_log(self) -> None:
        try:
            self.log_file.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session startup
# ---------------------------------------------------------------------------


def _build_session_env(
    *,
    worktree_path: Path,
    run_dir: Path,
    role: str,
    agent_label: str,
    issue_number: int,
    session_name: str,
    web_port: int | None,
) -> dict[str, str]:
    """Build full environment for a persistent PTY session."""
    completion_relpath = f".issue-orchestrator/sessions/{run_dir.name}/completion-{role}.json"
    overrides: dict[str, str] = {
        f"{ENV_PREFIX}COMPLETION_PATH": completion_relpath,
        f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR": str(run_dir),
        f"{ENV_PREFIX}AGENT_LABEL": agent_label,
        f"{ENV_PREFIX}ISSUE_NUMBER": str(issue_number),
        f"{ENV_PREFIX}RUN_DIR": str(run_dir),
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    }
    if web_port is not None:
        overrides["ORCHESTRATOR_API_PORT"] = str(web_port)

    overrides.update(build_runtime_tool_env(worktree_path, base_env={}))

    # Ensure orchestrator binaries (coding-done, reviewer-done) are on PATH
    orch_bin = str(Path(sys.executable).parent)
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    worktree_venv_bin = str(worktree_path / ".venv" / "bin")
    current_path = os.environ.get("PATH", "")
    overrides["PATH"] = f"{worktree_venv_bin}:{scripts_dir}:{orch_bin}:{current_path}"

    return build_filtered_env(overrides=overrides)


def _build_agent_command(
    agent: AgentConfig,
    *,
    initial_prompt: str,
    prompt_file: str,
) -> list[str]:
    """Build Claude command for a persistent PTY session."""
    provider_name = agent.provider or agent.ai_system
    if not provider_name:
        raise ValueError("Agent must have provider or ai_system configured")
    provider = get_provider(provider_name)

    kwargs = dict(agent.provider_args)

    # Build system prompt with reviewer-done instructions
    reviewer_done_docs = get_reviewer_done_instructions()
    system_prompt = (
        f"{reviewer_done_docs}\n\n"
        f"---\n\n"
        f"Read {prompt_file} for your task-specific instructions."
    )
    if provider_name == "claude-code":
        user_system_prompt = kwargs.pop("system_prompt", None)
        if user_system_prompt:
            system_prompt = f"{system_prompt}\n\n---\n\n{user_system_prompt}"
        kwargs["system_prompt"] = system_prompt
        kwargs.setdefault("permission_mode", agent.permission_mode or "bypassPermissions")
    else:
        initial_prompt = f"{system_prompt}\n\n---\n\n{initial_prompt}"

    return provider.build_command(prompt=initial_prompt, model=agent.model, **kwargs)


def _start_pty_session(
    *,
    role: str,
    agent: AgentConfig,
    worktree_path: Path,
    run_dir: Path,
    phase_dir: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    agent_label: str,
    web_port: int | None,
    initial_prompt: str,
    prompt_file: str,
) -> _PtySession:
    """Start a persistent PTY session for an agent."""
    env = _build_session_env(
        worktree_path=worktree_path,
        run_dir=run_dir,
        role=role,
        agent_label=agent_label,
        issue_number=issue_number,
        session_name=session_name,
        web_port=web_port,
    )

    command = _build_agent_command(
        agent,
        initial_prompt=initial_prompt,
        prompt_file=prompt_file,
    )

    # Wrap command in a login shell.  Claude Code needs a proper shell
    # environment (TERM, LANG, etc.) that pexpect alone doesn't provide.
    # This mirrors how provider_runner.py launches agents.
    shell = env.get("SHELL") or os.environ.get("SHELL") or "/bin/sh"
    shell_command = shlex.join(command)

    log_path = phase_dir / "agent-output.log"
    recording_path = phase_dir / "terminal-recording.jsonl"
    cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
    log_file = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[run_dir / "terminal-recording.jsonl"],
        mirror_path=log_path,
        initial_rows=rows,
        initial_cols=cols,
    )

    completion_relpath = env[f"{ENV_PREFIX}COMPLETION_PATH"]
    completion_abs = worktree_path / completion_relpath

    logger.info(
        "Starting persistent %s session: %s",
        role,
        shell_command[:200],
    )

    child = pexpect.spawn(
        shell,
        ["-lc", shell_command],
        cwd=str(worktree_path),
        env=env,  # type: ignore[arg-type]
        logfile=log_file,
        timeout=None,
        encoding="utf-8",
    )

    return _PtySession(
        role=role,
        child=child,
        log_file=log_file,
        log_path=log_path,
        completion_path=completion_abs,
    )


# ---------------------------------------------------------------------------
# Completion file watching
# ---------------------------------------------------------------------------


def _drain_pty(session: _PtySession) -> None:
    """Drain any available output from the PTY to prevent buffer deadlock.

    If nobody reads the PTY master, the ~4 KB kernel buffer fills up and
    the child process blocks on its next write.  Calling read_nonblocking
    pulls pending bytes out of the buffer (they are already captured by the
    pexpect logfile).
    """
    try:
        while True:
            session.child.read_nonblocking(size=4096, timeout=0)
    except pexpect.TIMEOUT:
        pass  # No more data available right now
    except pexpect.EOF:
        pass  # Session ended


def _wait_for_completion(
    session: _PtySession,
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Wait for completion file to appear and return its contents.

    Returns None if timeout expires or session dies before completion.

    IMPORTANT: We must continuously drain the PTY output while waiting.
    If the PTY buffer fills (~4 KB on macOS), the child process blocks
    on write and never reaches the point where it writes the completion
    file — a classic PTY deadlock.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # Drain PTY output to prevent buffer deadlock
        _drain_pty(session)

        # Check if completion file exists and is valid
        if session.completion_path.exists() and session.completion_path.stat().st_size > 0:
            try:
                data = json.loads(session.completion_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass  # Partial write, retry

        # Check if session died
        if not session.alive:
            logger.warning("Session %s exited before writing completion file", session.role)
            return None

        time.sleep(_COMPLETION_POLL_INTERVAL)

    logger.warning(
        "Timeout waiting for %s completion file: %s",
        session.role,
        session.completion_path,
    )
    return None


def _archive_completion(session: _PtySession, round_index: int) -> None:
    """Archive completion file for this round."""
    if not session.completion_path.exists():
        return
    archive_path = session.completion_path.with_name(
        f"completion-{session.role}-round-{round_index:03d}.json"
    )
    try:
        session.completion_path.rename(archive_path)
    except OSError:
        logger.debug("Failed to archive completion file: %s", session.completion_path)


def _completion_to_reviewer_response(
    data: dict[str, Any],
    raw_output: str | None = None,
) -> ReviewExchangeResponse:
    """Map a reviewer completion record to a ReviewExchangeResponse."""
    outcome = data.get("outcome", "")
    # reviewer-done writes "review_approved" / "review_changes_requested",
    # while the legacy completion format writes "approved" / "changes_requested".
    if outcome in ("approved", "review_approved"):
        return ReviewExchangeResponse(
            response_type="ok",
            response_text=data.get("review_summary") or data.get("summary", "Approved"),
            getting_closer=True,
            raw_json=data,
            raw_output=raw_output,
        )
    # changes_requested or anything else.
    # getting_closer=None (unknown) rather than False so that each
    # changes_requested round does not automatically count as
    # "no progress".  The reviewer finding *new* issues after the coder
    # addressed old ones is progress, not stagnation.
    return ReviewExchangeResponse(
        response_type="changes_requested",
        response_text=data.get("review_issues") or data.get("summary", "Changes requested"),
        getting_closer=None,
        raw_json=data,
        raw_output=raw_output,
    )


def _completion_to_coder_response(
    data: dict[str, Any],
    raw_output: str | None = None,
) -> ReviewExchangeResponse:
    """Map a coder completion record to a ReviewExchangeResponse."""
    outcome = data.get("outcome", "")
    if outcome in ("completed", "COMPLETED"):
        return ReviewExchangeResponse(
            response_type="ok",
            response_text=data.get("implementation") or data.get("summary", "Completed"),
            getting_closer=None,
            raw_json=data,
            raw_output=raw_output,
        )
    return ReviewExchangeResponse(
        response_type="error",
        response_text=data.get("blocked_reason") or data.get("summary", "Blocked"),
        getting_closer=False,
        raw_json=data,
        raw_output=raw_output,
    )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _write_prompt_file(
    exchange_dir: Path,
    round_index: int,
    role: str,
    prompt_text: str,
) -> Path:
    """Write a prompt file and return its path."""
    prompt_dir = exchange_dir / f"round-{round_index:03d}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{role}-prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return prompt_path


def _build_reviewer_prompt(
    *,
    issue_number: int,
    issue_title: str,
    round_index: int,
    last_coder_text: str | None,
    last_reviewer_text: str | None,
    require_validation: bool,
    run_dir: Path,
) -> str:
    """Build reviewer prompt for a round."""
    validation_note = ""
    if require_validation:
        validation_note = (
            "Validation is required. Only approve if validation-record.json exists "
            f"and passed in {run_dir}. If missing or failed, request changes "
            "asking the coder to run make validate via coding-done."
        )
    prior = ""
    if last_coder_text:
        prior += f"\nCoder response:\n{last_coder_text}\n"
    if last_reviewer_text:
        prior += f"\nPrevious review feedback:\n{last_reviewer_text}\n"

    return (
        f"You are the reviewer in a coder↔reviewer exchange for issue #{issue_number}: {issue_title}.\n"
        f"Round {round_index}.\n"
        f"{validation_note}\n"
        "Review the current worktree changes.\n"
        "Consider:\n"
        "A) the changes for this issue\n"
        "B) relevant context in the broader codebase\n"
        "C) any applicable .claude/skills guidance\n"
        "D) docs/ if needed for intended behavior\n"
        f"{prior}\n"
        "When your review is complete, report your verdict using:\n"
        "  reviewer-done approved --summary '...' --risk low|medium|high\n"
        "  reviewer-done changes_requested --issues '...' --risk low|medium|high\n"
        "\n"
        "After calling reviewer-done, STOP and wait. "
        "Do NOT exit. Do NOT take further actions. "
        "The orchestrator will send your next task.\n"
    )


def _build_coder_prompt(
    *,
    issue_number: int,
    issue_title: str,
    round_index: int,
    reviewer_feedback: str,
    run_dir: Path,
) -> str:
    """Build coder prompt for a round."""
    return (
        f"You are the coder in a review exchange for issue #{issue_number}: {issue_title}.\n"
        f"Round {round_index}.\n"
        "Review the feedback below and update the worktree accordingly.\n"
        "After making changes, run validation and report completion using:\n"
        "  coding-done completed --implementation '...' --problems '...'\n"
        "\n"
        "If you cannot fix the issues:\n"
        "  coding-done blocked --reason '...' --attempted '...'\n"
        "\n"
        f"Session output dir: {run_dir}\n"
        f"\nReviewer feedback:\n{reviewer_feedback}\n"
        "\n"
        "After calling coding-done, STOP and wait. "
        "Do NOT exit. Do NOT take further actions. "
        "The orchestrator will send your next task.\n"
    )


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _append_session_log(
    session_output: SessionOutput,
    run_dir: Path,
    *,
    round_index: int,
    role: str,
    section: str,
    content: str,
) -> None:
    """Append transcript content to the dedicated review-exchange transcript."""
    session_output.append_review_exchange_session_log_entry(
        run_dir,
        round_index=round_index,
        role=role,
        section=section,
        content=content,
    )


def _append_provider_runner_logs(
    run_dir: Path,
    *,
    round_index: int,
    role: str,
    completion_data: dict[str, Any] | None,
) -> None:
    """Write round summary to provider-runner logs for UI parity."""
    output_dir = run_dir / "provider-runner"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    summary = json.dumps(completion_data, indent=2) if completion_data else "(no completion)"
    header = f"[{timestamp}] round={round_index} role={role}\n"
    _append_text(output_dir / "stdout.log", header + summary + "\n\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_local_loop_exchange(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    worktree_path: Path,
    issue_number: int,
    issue_title: str,
    coder_label: str,
    reviewer_label: str,
    coder_agent: AgentConfig,
    reviewer_agent: AgentConfig,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    initial_validation_record_path: Path | None = None,
    web_port: int | None = None,
    events: EventSink | None = None,
    event_context: EventContext | None = None,
    on_started: Callable[[Path], None] | None = None,
) -> ReviewExchangeOutcome:
    """Run via-local-loop review exchange with persistent PTY sessions.

    This is the discrete abstraction for the via-local-loop exchange mode.
    Both coder and reviewer stay alive across review rounds, communicating
    via file-based prompts and structured completion files.
    """
    run_dir: Path | None = None
    run_id: str | None = None

    def _emit(event_name: EventName, payload: dict[str, Any]) -> None:
        if events is None or event_context is None:
            return
        enriched = dict(payload)
        if run_dir is not None:
            enriched["run_dir"] = str(run_dir)
        if run_id is not None:
            enriched["session_run_id"] = run_id
        events.publish(make_trace_event(event_name, event_context.enrich(enriched)))

    # Set up run directory
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_name = f"review-exchange-{issue_number}-{timestamp}"
    claude_project_dir = (
        Path.home() / ".claude" / "projects" / _escape_claude_project_path(worktree_path)
    )
    run = session_output.start_run(
        worktree_path,
        session_name,
        issue_number=issue_number,
        agent_label=coder_label,
        backend="subprocess",
        claude_log_dir=str(claude_project_dir),
        orchestrator_log=str(get_repo_log_path(worktree_path)),
    )
    run_dir = run.run_dir
    run_id = run.run_id
    _seed_validation_record(
        run_dir=run_dir,
        source_record_path=initial_validation_record_path,
        session_output=session_output,
    )
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    session_output.update_manifest(run_dir, {"review_exchange_dir": str(exchange_dir)})

    def _finalize_manifest(outcome: str) -> None:
        session_output.update_manifest(
            run_dir,
            {
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,
            },
        )

    if on_started is not None:
        on_started(run_dir)

    session_output.ensure_review_exchange_session_log(run_dir)
    _emit(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "session_name": session_name,
        "coder_label": coder_label,
        "reviewer_label": reviewer_label,
        "max_rounds": max_rounds,
        "max_no_progress": max_no_progress,
        "require_validation": require_validation,
        "exchange_dir": str(exchange_dir),
    })

    # Kill any existing Claude sessions in this worktree (e.g. the coder
    # session that just completed). Claude Code uses project-level locking
    # so a second session would hang during initialization.
    _kill_existing_claude_sessions(worktree_path)

    try:
        outcome = _run_exchange_rounds(
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            web_port=web_port,
            emit=_emit,
            session_output=session_output,
        )
        _finalize_manifest(outcome.status)
        return outcome
    except Exception as exc:
        _finalize_manifest("failed")
        _emit(EventName.REVIEW_EXCHANGE_FAILED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": 0,
            "error": str(exc),
            "exception_type": type(exc).__name__,
        })
        raise


def _run_phase(
    *,
    round_index: int,
    role: str,
    agent: AgentConfig,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    agent_label: str,
    web_port: int | None,
    prompt_file_path: Path,
) -> tuple[_PtySession, dict[str, Any] | None]:
    """Start a fresh PTY session for one phase and wait for completion.

    Claude Code's TUI cannot accept follow-up messages via PTY stdin, so
    each phase (reviewer or coder) gets its own process.  The initial
    prompt is passed on the command line, which Claude reads reliably.

    Returns (session, completion_data).  The caller must terminate the
    session when done.
    """
    phase_dir = exchange_dir / f"round-{round_index:03d}" / role
    phase_dir.mkdir(parents=True, exist_ok=True)
    initial_prompt = (
        f"You are the {role} in a review exchange for issue "
        f"#{issue_number}: {issue_title}. "
        f"Read your instructions at {prompt_file_path}. "
    )
    if role == "reviewer":
        initial_prompt += (
            "After reviewing, use `reviewer-done` to report your verdict."
        )
    else:
        initial_prompt += (
            "Address the feedback, run validation, then use "
            "`coding-done` to report completion."
        )

    session = _start_pty_session(
        role=role,
        agent=agent,
        worktree_path=worktree_path,
        run_dir=run_dir,
        phase_dir=phase_dir,
        issue_number=issue_number,
        issue_title=issue_title,
        session_name=session_name,
        agent_label=agent_label,
        web_port=web_port,
        initial_prompt=initial_prompt,
        prompt_file=str(prompt_file_path),
    )

    timeout = agent.timeout_minutes * 60
    data = _wait_for_completion(session, timeout_seconds=timeout)
    return session, data


def _run_exchange_rounds(  # noqa: PLR0913
    *,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    coder_label: str,
    reviewer_label: str,
    coder_agent: AgentConfig,
    reviewer_agent: AgentConfig,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    web_port: int | None,
    emit: Callable[[EventName, dict[str, Any]], None],
    session_output: SessionOutput,
) -> ReviewExchangeOutcome:
    """Execute the review exchange rounds.

    Each phase (reviewer, coder) gets a fresh PTY process because Claude
    Code's TUI cannot accept follow-up messages via PTY stdin.  Context
    from previous rounds is included in the prompt text.
    """
    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None

    for round_index in range(1, max_rounds + 1):
        # --- Reviewer phase ---
        reviewer_prompt_text = _build_reviewer_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            last_coder_text=last_coder_text,
            last_reviewer_text=last_reviewer_text,
            require_validation=require_validation,
            run_dir=run_dir,
        )
        reviewer_prompt_path = _write_prompt_file(
            exchange_dir, round_index, "reviewer", reviewer_prompt_text,
        )
        _append_session_log(
            session_output,
            run_dir,
            round_index=round_index,
            role="reviewer",
            section="prompt",
            content=reviewer_prompt_text,
        )
        emit(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
        })
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "reviewer",
            "prompt_chars": len(reviewer_prompt_text),
        })

        # Kill leftover sessions before each new phase
        _kill_existing_claude_sessions(worktree_path)

        reviewer_session, reviewer_data = _run_phase(
            round_index=round_index,
            role="reviewer",
            agent=reviewer_agent,
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=reviewer_label,
            web_port=web_port,
            prompt_file_path=reviewer_prompt_path,
        )

        try:
            if reviewer_data is None:
                _append_session_log(
                    session_output,
                    run_dir,
                    round_index=round_index,
                    role="reviewer",
                    section="completion",
                    content="(no completion - timeout or session died)",
                )
                emit(EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "role": "reviewer",
                    "reason": "no_completion",
                })
                summary = _write_summary(exchange_dir, round_index, reviewer_response=None)
                emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "rounds": round_index,
                    "status": "error",
                    "reason": "reviewer_no_completion",
                })
                return ReviewExchangeOutcome(
                    status="error",
                    rounds=round_index,
                    reason="reviewer_no_completion",
                    exchange_dir=exchange_dir,
                    summary=summary,
                )

            _append_provider_runner_logs(
                run_dir, round_index=round_index, role="reviewer", completion_data=reviewer_data,
            )
            reviewer_response = _completion_to_reviewer_response(reviewer_data)
            _archive_completion(reviewer_session, round_index)
        finally:
            reviewer_session.terminate()

        # Enforce validation if required
        if require_validation and reviewer_response.response_type == "ok":
            if not _validation_passed(run_dir):
                reviewer_response = ReviewExchangeResponse(
                    response_type="changes_requested",
                    response_text=(
                        "Validation record missing or failed. "
                        "Address the failing checks and continue."
                    ),
                    getting_closer=False,
                    raw_json=reviewer_data,
                    raw_output=reviewer_response.raw_output,
                )

        _write_round_log(
            exchange_dir=exchange_dir,
            round_index=round_index,
            role="reviewer",
            response=reviewer_response,
        )
        _append_session_log(
            session_output,
            run_dir,
            round_index=round_index,
            role="reviewer",
            section="completion",
            content=f"response_type={reviewer_response.response_type} "
                    f"text={reviewer_response.response_text}",
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "reviewer",
            "response_type": reviewer_response.response_type,
            "getting_closer": reviewer_response.getting_closer,
        })

        # Check if reviewer approved
        if reviewer_response.response_type == "ok":
            summary = _write_summary(exchange_dir, round_index, reviewer_response)
            emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "reviewer_response_type": reviewer_response.response_type,
                "coder_response_type": None,
            })
            emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "rounds": round_index,
                "status": "ok",
                "reason": "reviewer_ok",
            })
            return ReviewExchangeOutcome(
                status="ok",
                rounds=round_index,
                reason="reviewer_ok",
                reviewer_response=reviewer_response,
                exchange_dir=exchange_dir,
                summary=summary,
            )

        # Check no-progress
        if reviewer_response.getting_closer is False:
            no_progress_count += 1
        else:
            no_progress_count = 0

        if max_no_progress > 0 and no_progress_count >= max_no_progress:
            summary = _write_summary(exchange_dir, round_index, reviewer_response)
            emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "reviewer_response_type": reviewer_response.response_type,
                "coder_response_type": None,
            })
            emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "rounds": round_index,
                "status": "stopped",
                "reason": "reviewer_reports_no_progress",
            })
            return ReviewExchangeOutcome(
                status="stopped",
                rounds=round_index,
                reason="reviewer_reports_no_progress",
                reviewer_response=reviewer_response,
                exchange_dir=exchange_dir,
                summary=summary,
            )

        last_reviewer_text = reviewer_response.response_text

        # --- Coder phase ---
        coder_prompt_text = _build_coder_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            reviewer_feedback=reviewer_response.response_text,
            run_dir=run_dir,
        )
        coder_prompt_path = _write_prompt_file(
            exchange_dir, round_index, "coder", coder_prompt_text,
        )
        _append_session_log(
            session_output,
            run_dir,
            round_index=round_index,
            role="coder",
            section="prompt",
            content=coder_prompt_text,
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "coder",
            "prompt_chars": len(coder_prompt_text),
        })

        # Kill reviewer before starting coder
        _kill_existing_claude_sessions(worktree_path)

        coder_session, coder_data = _run_phase(
            round_index=round_index,
            role="coder",
            agent=coder_agent,
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=coder_label,
            web_port=web_port,
            prompt_file_path=coder_prompt_path,
        )

        try:
            if coder_data is None:
                _append_session_log(
                    session_output,
                    run_dir,
                    round_index=round_index,
                    role="coder",
                    section="completion",
                    content="(no completion - timeout or session died)",
                )
                emit(EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "role": "coder",
                    "reason": "no_completion",
                })
                summary = _write_summary(exchange_dir, round_index, reviewer_response)
                emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "reviewer_response_type": reviewer_response.response_type,
                    "coder_response_type": "error",
                })
                emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "rounds": round_index,
                    "status": "error",
                    "reason": "coder_no_completion",
                })
                return ReviewExchangeOutcome(
                    status="error",
                    rounds=round_index,
                    reason="coder_no_completion",
                    reviewer_response=reviewer_response,
                    exchange_dir=exchange_dir,
                    summary=summary,
                )

            _append_provider_runner_logs(
                run_dir, round_index=round_index, role="coder", completion_data=coder_data,
            )
            coder_response = _completion_to_coder_response(coder_data)
            _archive_completion(coder_session, round_index)
        finally:
            coder_session.terminate()

        _write_round_log(
            exchange_dir=exchange_dir,
            round_index=round_index,
            role="coder",
            response=coder_response,
        )
        _append_session_log(
            session_output,
            run_dir,
            round_index=round_index,
            role="coder",
            section="completion",
            content=f"response_type={coder_response.response_type} "
                    f"text={coder_response.response_text}",
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "coder",
            "response_type": coder_response.response_type,
            "getting_closer": coder_response.getting_closer,
        })

        emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "reviewer_response_type": reviewer_response.response_type,
            "coder_response_type": coder_response.response_type,
        })
        last_coder_text = coder_response.response_text

    # Exhausted max rounds
    summary = _write_summary(exchange_dir, max_rounds, reviewer_response=None)
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": max_rounds,
        "status": "stopped",
        "reason": "max_rounds_exceeded",
    })
    return ReviewExchangeOutcome(
        status="stopped",
        rounds=max_rounds,
        reason="max_rounds_exceeded",
        exchange_dir=exchange_dir,
        summary=summary,
    )
