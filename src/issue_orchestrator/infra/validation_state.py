"""Validation retry state management.

Manages persistent state for validation retry flow. State is stored in the
worktree filesystem, making it durable across orchestrator restarts.

Files:
- .issue-orchestrator/validation-state.json: Retry count, timestamps, config
- .issue-orchestrator/validation-errors.txt: Human-readable error output
- .issue-orchestrator/retry-prompt.md: Prompt for retry session
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VALIDATION_STATE_DIR = ".issue-orchestrator"
VALIDATION_STATE_FILE = "validation-state.json"
VALIDATION_ERRORS_FILE = "validation-errors.txt"
RETRY_PROMPT_FILE = "retry-prompt.md"


@dataclass
class ValidationState:
    """Persistent validation retry state.

    Stored in worktree at .issue-orchestrator/validation-state.json
    """
    retry_count: int = 0
    max_retries: int = 3
    validation_cmd: Optional[str] = None
    last_error: Optional[str] = None
    last_error_file: Optional[str] = None
    original_prompt_file: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def increment_retry(self) -> "ValidationState":
        """Return new state with incremented retry count."""
        return ValidationState(
            retry_count=self.retry_count + 1,
            max_retries=self.max_retries,
            validation_cmd=self.validation_cmd,
            last_error=self.last_error,
            last_error_file=self.last_error_file,
            original_prompt_file=self.original_prompt_file,
            created_at=self.created_at,
            updated_at=_now_iso(),
        )

    @property
    def retries_remaining(self) -> int:
        """Number of retries still available."""
        return max(0, self.max_retries - self.retry_count)

    @property
    def can_retry(self) -> bool:
        """Whether more retries are allowed."""
        return self.retry_count < self.max_retries


def _now_iso() -> str:
    """Current time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _state_dir(worktree_path: Path) -> Path:
    """Get the .issue-orchestrator directory."""
    return worktree_path / VALIDATION_STATE_DIR


def _state_file(worktree_path: Path) -> Path:
    """Get the validation-state.json path."""
    return _state_dir(worktree_path) / VALIDATION_STATE_FILE


def _errors_file(worktree_path: Path) -> Path:
    """Get the validation-errors.txt path."""
    return _state_dir(worktree_path) / VALIDATION_ERRORS_FILE


def _retry_prompt_file(worktree_path: Path) -> Path:
    """Get the retry-prompt.md path."""
    return _state_dir(worktree_path) / RETRY_PROMPT_FILE


def read_validation_state(worktree_path: Path) -> Optional[ValidationState]:
    """Read validation state from worktree.

    Returns None if no state file exists (not in retry flow).
    """
    state_file = _state_file(worktree_path)
    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text())
        return ValidationState(
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            validation_cmd=data.get("validation_cmd"),
            last_error=data.get("last_error"),
            last_error_file=data.get("last_error_file"),
            original_prompt_file=data.get("original_prompt_file"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read validation state from %s: %s", state_file, e)
        return None


def write_validation_state(worktree_path: Path, state: ValidationState) -> Path:
    """Write validation state to worktree.

    Returns the path to the state file.
    """
    state_dir = _state_dir(worktree_path)
    state_dir.mkdir(parents=True, exist_ok=True)

    state_file = _state_file(worktree_path)

    # Update timestamp
    data = asdict(state)
    if not data.get("created_at"):
        data["created_at"] = _now_iso()
    data["updated_at"] = _now_iso()

    state_file.write_text(json.dumps(data, indent=2))
    logger.info("Wrote validation state to %s (retry_count=%d)", state_file, state.retry_count)
    return state_file


def write_validation_errors(
    worktree_path: Path,
    validation_cmd: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> Path:
    """Write validation errors to worktree.

    Returns the path to the errors file.
    """
    state_dir = _state_dir(worktree_path)
    state_dir.mkdir(parents=True, exist_ok=True)

    errors_file = _errors_file(worktree_path)

    content = f"""=== VALIDATION FAILED ===
Command: {validation_cmd}
Exit code: {exit_code}
Timestamp: {_now_iso()}

=== STDERR ===
{stderr}

=== STDOUT ===
{stdout}
"""
    errors_file.write_text(content)
    logger.info("Wrote validation errors to %s", errors_file)
    return errors_file


def write_retry_prompt(
    worktree_path: Path,
    original_prompt: str,
    validation_cmd: str,
    validation_error: str,
    retry_count: int,
    max_retries: int,
) -> Path:
    """Write retry prompt to worktree.

    The retry prompt includes:
    - Original task context
    - Validation command that failed
    - Error output
    - Instructions for fixing

    Returns the path to the retry prompt file.
    """
    state_dir = _state_dir(worktree_path)
    state_dir.mkdir(parents=True, exist_ok=True)

    retry_prompt_path = _retry_prompt_file(worktree_path)
    errors_file = _errors_file(worktree_path)

    content = f"""# Validation Retry (Attempt {retry_count + 1}/{max_retries + 1})

The codebase was working before you started. After your changes, validation failed.

## Original Task

{original_prompt}

## Validation Failure

Command: `{validation_cmd}`

The full error output is available at: `{errors_file}`

### Error Summary

```
{validation_error[:2000]}
```

## Instructions

Fix these errors. The failures may be directly in code you changed, or transitively
related (e.g., you broke an import, renamed something other code depends on, or
violated a project guardrail).

When done, run: `agent-done completed --implementation "..." --problems "..."`
"""
    retry_prompt_path.write_text(content)
    logger.info("Wrote retry prompt to %s", retry_prompt_path)
    return retry_prompt_path


def clear_validation_state(worktree_path: Path) -> None:
    """Clear validation state from worktree.

    Called when validation passes or max retries exhausted.
    """
    state_file = _state_file(worktree_path)
    retry_prompt = _retry_prompt_file(worktree_path)

    for path in [state_file, retry_prompt]:
        if path.exists():
            try:
                path.unlink()
                logger.debug("Removed %s", path)
            except OSError as e:
                logger.warning("Failed to remove %s: %s", path, e)

    # Keep validation-errors.txt for debugging


def has_pending_retry(worktree_path: Path) -> bool:
    """Check if worktree has a pending validation retry.

    Used during crash recovery to detect issues that were mid-retry.
    """
    state = read_validation_state(worktree_path)
    return state is not None and state.can_retry


def get_retry_prompt_path(worktree_path: Path) -> Optional[Path]:
    """Get path to retry prompt if it exists."""
    retry_prompt = _retry_prompt_file(worktree_path)
    return retry_prompt if retry_prompt.exists() else None
