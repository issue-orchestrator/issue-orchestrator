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

from ..domain.artifact_contracts import (
    ValidationFailed,
    ValidationPassed,
    ValidationRetry,
)
from ..domain.run_manifest import RunManifest
from ..domain.session_key import TaskKind

logger = logging.getLogger(__name__)
_NO_CURRENT_RETRY = object()

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
#   {retries_remaining} - How many attempts are left after this one
DEFAULT_RETRY_TEMPLATE = """# Validation Retry (Attempt {retry_count}/{max_retries}) — {retries_remaining} attempt(s) remaining after this

Your changes broke validation. The codebase was working before you started.

## Diagnosis Strategy

Before making changes, understand what went wrong:

1. **Read the errors below** — identify the root cause, not just the first symptom
2. **Check your diff** — run `git diff HEAD~1` to see exactly what you changed
3. **Trace the failure** — if a test fails, find the code path; if an import fails, check the dependency chain
4. **Run validation locally** — `{validation_cmd}` — confirm your fix works before calling coding-done

## Original Task

{original_task}

## Validation Failure

Command: `{validation_cmd}`
Full error output: `{error_file}`

```
{error_summary}
```

## Common Root Causes

- **Renamed/moved** a function, class, or module but missed callers, tests, or re-exports
- **Circular import** — a new import creates A→B→A
- **Type mismatch** — changed a return type or parameter without updating all call sites
- **Missing export** — added a new module but forgot `__init__.py` re-export
- **Test fixture** — changed production code but a test fixture still uses the old shape

## Completion

When you've fixed the errors, run:
```
coding-done completed --implementation "describe what you fixed" --problems "any remaining issues"
```

If you genuinely cannot fix the problem after careful investigation, run:
```
coding-done blocked --reason "the specific error and why you cannot resolve it" --attempted "what you tried"
```
"""


@dataclass
class ValidationState:
    """Persistent validation retry state.

    Stored in worktree at .issue-orchestrator/validation-state.json
    """
    retry_count: int = 0  # Queued retry attempt number, not completed retry count.
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
        """Whether the queued retry attempt is within the retry budget."""
        return self.max_retries > 0 and self.retry_count <= self.max_retries


@dataclass(frozen=True)
class ValidationRetryArtifacts:
    """Durable retry state and its associated artifact paths.

    ``source_task`` is the *required, concrete, non-review* task kind that owns
    this retry. The owner that builds the artifact resolves provenance so the
    retry queue never has to guess (see issue #6426):

    - Run-scoped artifacts are only constructed once the run directory's identity
      classifies to a concrete non-review ``TaskKind`` (CODE/REWORK/...). An
      unrecognized or review-only run is refused upstream, never returned with an
      unknown source.
    - Legacy worktree-level state (no run directory, predates run-scoped
      identity) is stamped ``TaskKind.CODE`` explicitly at construction, since
      that machinery only ever ran for coding work.

    There is no ``None`` / ``or TaskKind.CODE`` fallback: the field is always a
    valid coding-side task, so a review-only or unknown-provenance artifact can
    never be relaunched as coding work.
    """

    state: ValidationState
    state_path: Path
    source_task: TaskKind
    retry_prompt_path: Path | None = None


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


def _sessions_dir(worktree_path: Path) -> Path:
    return _state_dir(worktree_path) / "sessions"


def _run_state_file(run_dir: Path) -> Path:
    return run_dir / VALIDATION_STATE_FILE


def _run_retry_prompt_file(run_dir: Path) -> Path:
    return run_dir / RETRY_PROMPT_FILE


def _load_validation_state_file(state_file: Path) -> Optional[ValidationState]:
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


def _run_activity_mtime(run_dir: Path) -> float:
    candidates = [
        run_dir,
        run_dir / "manifest.json",
        _run_state_file(run_dir),
        _run_retry_prompt_file(run_dir),
    ]
    return max((path.stat().st_mtime for path in candidates if path.exists()), default=0.0)


def _run_dirs_newest_first(worktree_path: Path) -> list[Path]:
    sessions_dir = _sessions_dir(worktree_path)
    if not sessions_dir.exists():
        return []

    run_dirs = [
        path
        for path in sessions_dir.iterdir()
        if path.is_dir() and "__" in path.name
    ]
    return sorted(run_dirs, key=_run_activity_mtime, reverse=True)


def _run_validation_status(run_dir: Path) -> str | None:
    """Return ``"passed" | "failed" | "retry"`` from the typed outcome.

    Routes through ``RunManifest.validation_outcome`` rather than reading
    the raw ``validation_status`` field — same on-disk format, but the
    typed property's read-time inconsistency tolerance (legacy triple
    with status="passed" + stale reason → ``ValidationPassed``) means
    this caller can never observe an inconsistent state.
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return None

    try:
        manifest = RunManifest.load(run_dir)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read run manifest from %s: %s", manifest_path, e)
        return None

    outcome = manifest.validation_outcome
    if isinstance(outcome, ValidationPassed):
        return "passed"
    if isinstance(outcome, ValidationFailed):
        return "failed"
    if isinstance(outcome, ValidationRetry):
        return "retry"
    return None


def _run_session_name(run_dir: Path) -> str:
    if "__" not in run_dir.name:
        return run_dir.name
    return run_dir.name.split("__", 1)[1]


def _run_source_task(run_dir: Path) -> TaskKind | None:
    """Classify the task that produced this run directory from its identity."""
    return TaskKind.from_session_name(_run_session_name(run_dir))


def _run_is_review_only(run_dir: Path) -> bool:
    task = _run_source_task(run_dir)
    return task is not None and task.is_review_only


def _run_can_supersede_retry_state(run_dir: Path) -> bool:
    return _run_session_name(run_dir).startswith(("coding-", "issue-", "rework-"))


def _find_run_scoped_retry_artifacts(
    worktree_path: Path,
) -> ValidationRetryArtifacts | object | None:
    for run_dir in _run_dirs_newest_first(worktree_path):
        # A review-only run (PR review / retrospective review) makes no commits and
        # publishes nothing. It must be fully transparent to coding-retry recovery:
        # any validation-state.json under such a run — e.g. left by the pre-fix
        # retrospective-review bug or a crash boundary — must never be recovered as
        # a coding validation retry (it would relaunch as TaskKind.CODE and open a
        # PR on an empty branch, issue #6426), and its terminal pass/fail status
        # must not suppress a genuine coding retry in an older run. Skip it entirely.
        if _run_is_review_only(run_dir):
            continue

        validation_status = _run_validation_status(run_dir)
        if validation_status in {"passed", "failed"}:
            return _NO_CURRENT_RETRY

        state_path = _run_state_file(run_dir)
        state = _load_validation_state_file(state_path)
        if state is not None:
            source_task = _run_source_task(run_dir)
            if source_task is None:
                # Unrecognized run identity (not in the classifier) with retry
                # state. Provenance is unknown, so we fail safe: refuse to recover
                # it as a coding retry rather than coerce it to TaskKind.CODE and
                # risk relaunching unknown work on an empty/wrong branch (#6426).
                # Keep scanning older runs for a genuine, classifiable coding retry.
                logger.warning(
                    "Ignoring run-scoped validation retry with unrecognized identity %r; "
                    "provenance is unknown so it will not be recovered as a coding retry",
                    _run_session_name(run_dir),
                )
                continue
            prompt_path = _run_retry_prompt_file(run_dir)
            return ValidationRetryArtifacts(
                state=state,
                state_path=state_path,
                source_task=source_task,
                retry_prompt_path=prompt_path if prompt_path.exists() else None,
            )

        if _run_retry_prompt_file(run_dir).exists():
            continue

        if _run_can_supersede_retry_state(run_dir):
            return _NO_CURRENT_RETRY

    return None


def find_pending_retry_artifacts(worktree_path: Path) -> ValidationRetryArtifacts | None:
    """Find durable validation retry artifacts for a worktree.

    Current sessions write retry state inside the run directory. Older versions
    wrote it directly under ``.issue-orchestrator``; keep that legacy location as
    a compatibility fallback.
    """
    run_scoped = _find_run_scoped_retry_artifacts(worktree_path)
    if run_scoped is _NO_CURRENT_RETRY:
        return None
    if isinstance(run_scoped, ValidationRetryArtifacts):
        return run_scoped

    legacy_state_path = _state_file(worktree_path)
    legacy_state = _load_validation_state_file(legacy_state_path)
    if legacy_state is None:
        return None

    legacy_prompt_path = _retry_prompt_file(worktree_path)
    # Legacy worktree-level state predates run-scoped identity. That machinery
    # only ever ran for coding work, so its provenance is CODE by construction —
    # stamped explicitly here so recovery never has to infer it (#6426).
    return ValidationRetryArtifacts(
        state=legacy_state,
        state_path=legacy_state_path,
        source_task=TaskKind.CODE,
        retry_prompt_path=legacy_prompt_path if legacy_prompt_path.exists() else None,
    )


def read_validation_state(worktree_path: Path) -> Optional[ValidationState]:
    """Read validation state from worktree.

    Returns None if no current state file exists (not in retry flow).
    """
    artifacts = find_pending_retry_artifacts(worktree_path)
    return artifacts.state if artifacts is not None else None


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


def _truncate_with_tail(text: str, max_length: int = 4000, tail_length: int = 2000) -> str:
    """Truncate text keeping both head and tail.

    For test output, the summary (pass/fail counts, failure details) is at the end.
    This function keeps the last `tail_length` chars which contain the important info.

    Args:
        text: The text to truncate
        max_length: Maximum total length
        tail_length: How much of the end to preserve

    Returns:
        Truncated text with "[...truncated...]" marker if needed
    """
    if len(text) <= max_length:
        return text

    # Keep tail_length from the end (has the summary)
    # Use remaining budget for the head
    head_length = max_length - tail_length - 30  # 30 chars for marker
    if head_length < 100:
        # If not enough room for meaningful head, just show tail
        return f"[...truncated {len(text) - tail_length} chars...]\n\n{text[-tail_length:]}"

    return f"{text[:head_length]}\n\n[...truncated {len(text) - head_length - tail_length} chars...]\n\n{text[-tail_length:]}"


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
        validation_error: Error output (will be truncated, preserving tail)
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
    # Use _truncate_with_tail to preserve the end (pytest summary is at the end)
    display_count = retry_count + 1
    display_max = max_retries + 1
    content = template.format(
        original_task=original_prompt,
        validation_cmd=validation_cmd,
        error_file=str(errors_file),
        error_summary=_truncate_with_tail(validation_error),
        retry_count=display_count,
        max_retries=display_max,
        retries_remaining=display_max - display_count,
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
    artifacts = find_pending_retry_artifacts(worktree_path)
    if artifacts is not None and artifacts.retry_prompt_path is not None:
        return artifacts.retry_prompt_path
    legacy_prompt = _retry_prompt_file(worktree_path)
    return legacy_prompt if legacy_prompt.exists() else None
