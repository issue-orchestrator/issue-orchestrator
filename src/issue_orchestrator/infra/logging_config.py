"""Centralized logging configuration.

This module provides a single setup function for consistent logging across
the application. Logs are for human consumption (debugging, ops) and should
NOT be parsed by UI or tests - use events for that.

Logger naming convention:
- issue_orchestrator.orchestrator
- issue_orchestrator.control.planner
- issue_orchestrator.control.action_applier
- issue_orchestrator.execution.*
- issue_orchestrator.adapters.github

Levels:
- DEBUG: Internal details, payload dumps, retry backoff
- INFO: Major milestones (started, plan computed, applied N steps, idle)
- WARNING: Recoverable problems / degraded mode
- ERROR: Failed step or invariant breach / exception with traceback
- CRITICAL: Process exiting due to fatal configuration or invariant breach

Context fields (via extra=):
- run_id: UUID for orchestrator process lifetime
- tick_id: Monotonically increasing tick counter
- issue_key: Stable issue key like "M1-011"
- session_id: Session identifier

Log file location:
- {repo_root}/.issue-orchestrator/state/logs/orchestrator.log

repo_root is REQUIRED - it's derived from the config file location.
One orchestrator = one repo = one log file. No fallback.
"""

import logging
import os
from pathlib import Path
from typing import Any

# Flag to track if logging has been set up (for idempotency)
_logging_configured = False
_current_log_file: Path | None = None


def get_repo_log_path(repo_root: Path | str) -> Path:
    """Get the repo-scoped log file path.

    Args:
        repo_root: Path to the repository root

    Returns:
        Path to {repo_root}/.issue-orchestrator/state/logs/orchestrator.log
    """
    repo_path = Path(repo_root).resolve()
    log_dir = repo_path / ".issue-orchestrator" / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "orchestrator.log"


class ContextFormatter(logging.Formatter):
    """Formatter that includes context fields from extra= if present."""

    def format(self, record: logging.LogRecord) -> str:
        # Build context string from known extra fields
        context_parts = []
        for field in ("run_id", "tick_id", "issue_key", "session_id", "step_id"):
            value = getattr(record, field, None)
            if value is not None:
                # Shorten run_id for readability
                if field == "run_id" and len(str(value)) > 8:
                    value = str(value)[:8]
                context_parts.append(f"{field}={value}")

        if context_parts:
            record.context = " [" + " ".join(context_parts) + "]"
        else:
            record.context = ""

        return super().format(record)


def setup_logging(
    repo_root: Path | str,
    level: str = "INFO",
    console_output: bool = False,
    log_file: Path | None = None,
    json_format: bool = False,
) -> Path | None:
    """Configure logging for the application.

    This function is idempotent - calling it multiple times will not
    duplicate handlers.

    Args:
        repo_root: Repository root (REQUIRED). Logs go to
            {repo_root}/.issue-orchestrator/state/logs/orchestrator.log
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        console_output: If True, also log to stderr
        log_file: Path to log file (overrides repo_root-based path)
        json_format: If True, use JSON format (for structured log aggregation)

    Returns:
        Path to the log file being used, or None if file logging failed
    """
    global _logging_configured, _current_log_file

    # Determine target log file
    env_log_file = os.environ.get("ORCHESTRATOR_LOG_FILE")
    if log_file is not None:
        target_log_file = log_file
    elif env_log_file:
        target_log_file = Path(env_log_file)
    else:
        target_log_file = get_repo_log_path(repo_root)

    # Idempotent - if already configured with same file, return early
    if _logging_configured and _current_log_file == target_log_file:
        return _current_log_file

    # If already configured but switching log files (shouldn't happen), reset first
    if _logging_configured and _current_log_file != target_log_file:
        logging.warning("Unexpected log file switch from %s to %s", _current_log_file, target_log_file)
        reset_logging()

    log_file = target_log_file
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any existing handlers (in case of test isolation issues)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Human-readable format with context
    human_format = "%(asctime)s [%(process)d] %(name)s %(levelname)s:%(context)s %(message)s"
    # Simpler format for stderr
    stderr_format = "[%(process)d] %(name)s: %(message)s"

    # File handler - preferred but can fall back if not writable
    file_handler = None
    fallback_used = False
    try:
        file_handler = logging.FileHandler(log_file, mode="a")
    except OSError:
        fallback_path = Path("/tmp/issue-orchestrator.log")
        try:
            file_handler = logging.FileHandler(fallback_path, mode="a")
            log_file = fallback_path
            fallback_used = True
        except OSError:
            file_handler = None

    if file_handler:
        file_handler.setLevel(log_level)
        file_handler.setFormatter(ContextFormatter(human_format))
        root_logger.addHandler(file_handler)

    # Stderr handler - conditional
    if console_output or os.environ.get("ORCHESTRATOR_LOG_TO_STDERR") == "1":
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(logging.INFO)
        stderr_handler.setFormatter(logging.Formatter(stderr_format))
        # Only log events logger or WARNING+ to stderr (not all debug noise)
        stderr_handler.addFilter(
            lambda record: "events" in record.name or record.levelno >= logging.WARNING
        )
        root_logger.addHandler(stderr_handler)

    _logging_configured = True
    _current_log_file = log_file if file_handler else None

    # Log startup marker
    if fallback_used:
        logging.warning("Log file not writable, using fallback: %s", log_file)
    if not file_handler:
        logging.warning("Log file unavailable, logging to stderr only")
    logging.info("=" * 50)
    logging.info("issue-orchestrator logging initialized (level=%s, log_file=%s)", level, log_file)

    return _current_log_file


def reset_logging() -> None:
    """Reset logging configuration. For testing or switching log files."""
    global _logging_configured, _current_log_file
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    _logging_configured = False
    _current_log_file = None


def log_context(**kwargs: Any) -> dict[str, Any]:
    """Build an extra dict for logging with context fields.

    Usage:
        logger.info("Applied step", extra=log_context(tick_id=1, issue_key="M1-011"))

    Args:
        **kwargs: Context fields (run_id, tick_id, issue_key, session_id, step_id)

    Returns:
        Dict suitable for logger's extra= parameter
    """
    return {k: v for k, v in kwargs.items() if v is not None}
