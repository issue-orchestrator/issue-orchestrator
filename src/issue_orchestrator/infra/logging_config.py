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

Log rotation:
- Rotates daily at midnight
- Retention days configurable via config.log_retention_days (default 7)
- Old logs are named: orchestrator.log.YYYY-MM-DD

repo_root is REQUIRED - it's derived from the config file location.
One orchestrator = one repo = one log file. No fallback.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
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


def get_control_center_log_path() -> Path:
    """Global, repo-independent log file for the Control Center process.

    The Control Center manages orchestrators across many repos and has no single
    ``repo_root``, so its log lives under the user's home. Without it, the CC's
    supervisor records (start/stop/kill of orchestrator instances) only reach
    stderr — the terminal it was launched from — and are lost the moment that
    terminal closes, making cross-repo orchestrator lifecycle events impossible
    to reconstruct after the fact.
    """
    log_dir = Path.home() / ".issue-orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "control-center.log"


def add_rotating_file_handler(log_file: Path, *, level: int) -> bool:
    """Attach a daily-rotating file handler to the root logger.

    Idempotent: a second call for the same file is a no-op. Returns ``True`` if a
    handler was added, ``False`` if one for this file already existed.
    """
    root = logging.getLogger()
    target = str(log_file)
    for existing in root.handlers:
        if (
            isinstance(existing, TimedRotatingFileHandler)
            and getattr(existing, "baseFilename", None) == target
        ):
            return False
    handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=7, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    return True


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


def _determine_log_file(log_file: Path | None, repo_root: Path | str) -> Path:
    """Determine the target log file path."""
    env_log_file = os.environ.get("ORCHESTRATOR_LOG_FILE")
    if log_file is not None:
        return log_file
    if env_log_file:
        return Path(env_log_file)
    return get_repo_log_path(repo_root)


def _create_file_handler(
    log_file: Path, log_retention_days: int
) -> tuple[TimedRotatingFileHandler | None, Path, bool]:
    """Create file handler with fallback. Returns (handler, final_path, fallback_used)."""
    try:
        handler = TimedRotatingFileHandler(
            str(log_file), when="midnight", interval=1,
            backupCount=log_retention_days, encoding="utf-8",
        )
        return handler, log_file, False
    except OSError:
        fallback_path = Path("/tmp/issue-orchestrator.log")
        try:
            handler = TimedRotatingFileHandler(
                str(fallback_path), when="midnight", interval=1,
                backupCount=log_retention_days, encoding="utf-8",
            )
            return handler, fallback_path, True
        except OSError:
            return None, log_file, False


# Third-party loggers that emit one INFO line per network call. httpx logs
# every GitHub request (and the fetch layer refreshes "hot" issues from several
# hot-lists per cycle, so a single issue can be logged multiple times a tick).
# At INFO this buries the orchestrator's own signal — a real incident produced a
# 318 MB log that was ~95% httpx lines, drowning the "[LOOP] Tick took 153.9s"
# warning that explained a stall. These libraries are not part of our log/event
# contract, so we pin them to WARNING. The Control Center set this for its own
# process only; doing it here makes every entrypoint (engine included) inherit it.
_NOISY_THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


def _quiet_noisy_third_party_loggers() -> None:
    """Pin chatty third-party loggers to WARNING so the orchestrator log stays readable."""
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(
    repo_root: Path | str,
    level: str = "INFO",
    console_output: bool = False,
    log_file: Path | None = None,
    json_format: bool = False,
    log_retention_days: int = 7,
) -> Path | None:
    """Configure logging for the application."""
    global _logging_configured, _current_log_file

    target_log_file = _determine_log_file(log_file, repo_root)

    if _logging_configured and _current_log_file == target_log_file:
        return _current_log_file

    if _logging_configured and _current_log_file != target_log_file:
        logging.warning("Unexpected log file switch from %s to %s", _current_log_file, target_log_file)
        reset_logging()

    log_file = target_log_file
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    human_format = "%(asctime)s [%(process)d] %(name)s %(levelname)s:%(context)s %(message)s"
    stderr_format = "[%(process)d] %(name)s: %(message)s"

    file_handler, log_file, fallback_used = _create_file_handler(log_file, log_retention_days)

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

    _quiet_noisy_third_party_loggers()

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


def issue_log(issue_number: int, message: str) -> str:
    """Prefix message with issue number for easy grepping.

    Usage:
        logger.info(issue_log(123, "Session starting: type=%s"), task_type)

    This produces: "[issue-123] Session starting: type=code"

    Grep all logs for an issue:
        grep "\\[issue-123\\]" orchestrator.log

    Args:
        issue_number: The GitHub issue number
        message: The log message (can contain format placeholders)

    Returns:
        Message prefixed with [issue-N]
    """
    return f"[issue-{issue_number}] {message}"
