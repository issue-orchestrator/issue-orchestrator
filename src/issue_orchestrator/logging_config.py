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
"""

import logging
import os
from pathlib import Path
from typing import Any

# Default log file location
DEFAULT_LOG_FILE = Path.home() / ".issue-orchestrator.log"

# Flag to track if logging has been set up (for idempotency)
_logging_configured = False


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
    level: str = "INFO",
    console_output: bool = False,
    log_file: Path | None = None,
    json_format: bool = False,
) -> None:
    """Configure logging for the application.

    This function is idempotent - calling it multiple times will not
    duplicate handlers.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        console_output: If True, also log to stderr
        log_file: Path to log file. Defaults to ~/.issue-orchestrator.log
        json_format: If True, use JSON format (for structured log aggregation)
    """
    global _logging_configured

    # Make this idempotent
    if _logging_configured:
        return

    log_file = log_file or DEFAULT_LOG_FILE
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

    # File handler - always enabled
    file_handler = logging.FileHandler(log_file, mode="a")
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

    # Log startup marker
    logging.info("=" * 50)
    logging.info("issue-orchestrator logging initialized (level=%s)", level)


def reset_logging() -> None:
    """Reset logging configuration. For testing only."""
    global _logging_configured
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    _logging_configured = False


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
