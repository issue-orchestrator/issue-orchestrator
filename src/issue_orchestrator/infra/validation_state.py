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

# Default retry prompt template. Users can override by setting retry.retry_prompt_template
# or per-agent retry_prompt_template in their config.
# Template variables:
#   {original_task} - The original task/prompt
#   {validation_cmd} - The command that failed
#   {error_file} - Path to the full error output
#   {error_summary} - Truncated error output
#   {retry_count} - Current attempt number (1-based)
#   {max_retries} - Total allowed attempts
DEFAULT_RETRY_TEMPLATE = """# Validation Retry (Attempt {retry_count}/{max_retries})

The codebase was working before you started. After your changes, validation failed.

## Original Task

{original_task}

## Validation Failure

Command: `{validation_cmd}`

The full error output is available at: `{error_file}`

### Error Summary

```
{error_summary}
```

## Instructions

Fix these validation errors. The failures may be:
- Directly in code you changed
- Transitively related (broken imports, renamed dependencies, violated guardrails)

When you've fixed the errors, run:
```
agent-done completed --implementation "describe what you fixed" --problems "any remaining issues"
```

If you cannot fix the problem (e.g., it requires human decision, external dependency, or unclear requirements), run:
```
agent-done blocked --reason "explain why you're blocked"
```
"""


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
    template_path: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """Write retry prompt to worktree.

    Renders a retry prompt template with validation error context and writes
    it to the worktree. The prompt instructs the agent to fix validation errors.

    Args:
        worktree_path: Path to the worktree
        original_prompt: The original task prompt
        validation_cmd: The validation command that failed
        validation_error: Error output (will be truncated to 2000 chars)
        retry_count: Current retry attempt (0-based, displayed as 1-based)
        max_retries: Maximum allowed retries
        template_path: Optional path to custom template (relative to repo_root)
        repo_root: Repo root for resolving template_path (required if template_path set)

    Returns:
        Path to the written retry prompt file.
    """
    state_dir = _state_dir(worktree_path)
    state_dir.mkdir(parents=True, exist_ok=True)

    retry_prompt_path = _retry_prompt_file(worktree_path)
    errors_file = _errors_file(worktree_path)

    # Load template - custom or default
    template = DEFAULT_RETRY_TEMPLATE
    if template_path and repo_root:
        template_full_path = repo_root / template_path
        if template_full_path.exists():
            try:
                template = template_full_path.read_text()
                logger.debug("Loaded retry template from %s", template_full_path)
            except OSError as e:
                logger.warning("Failed to load retry template from %s: %s", template_full_path, e)
        else:
            logger.warning("Retry template not found at %s, using default", template_full_path)

    # Render template with variables
    # Note: retry_count is 0-based internally, display as 1-based
    content = template.format(
        original_task=original_prompt,
        validation_cmd=validation_cmd,
        error_file=str(errors_file),
        error_summary=validation_error[:2000],
        retry_count=retry_count + 1,
        max_retries=max_retries + 1,
    )

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
