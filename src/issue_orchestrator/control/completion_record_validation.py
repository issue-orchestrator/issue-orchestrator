"""Completion record loading and worktree validation."""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.models import COMPLETION_RECORD_PATH, CompletionRecord, RequestedAction, sanitize_agent_label
from ..infra.runtime_artifacts import filter_runtime_managed_dirty_paths

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)
_DIRTY_FILES_REASON_LIMIT = 8

# Hard cap on the completion record file size before we call ``json.load``.
# Real records are <= a few KB; anything approaching this cap is almost
# certainly abusive or broken. Checking the file size first prevents a
# hostile agent from exhausting memory / CPU by writing, say, a 500 MB
# JSON blob and forcing the orchestrator's parser to walk it. Matches the
# per-field cap in CompletionRecord.from_dict so a well-formed record
# cannot exceed a small multiple of this.
#
# 2 MiB is roughly two orders of magnitude above the largest legitimate
# completion we have seen; tighten further if we ever shrink per-field
# caps.
_MAX_COMPLETION_FILE_BYTES = 2 * 1024 * 1024


class WorktreeValidationFailure(Enum):
    """Typed classification for publish-precondition failures."""

    CURRENT_BRANCH_UNKNOWN = "current_branch_unknown"
    PROTECTED_BRANCH = "protected_branch"
    DIRTY_POLICY = "dirty_policy"


@dataclass(frozen=True)
class WorktreeValidationResult:
    ok: bool
    reason: str = ""
    failure: WorktreeValidationFailure | None = None

    @classmethod
    def pass_(cls) -> "WorktreeValidationResult":
        return cls(ok=True)

    @classmethod
    def fail(
        cls,
        failure: WorktreeValidationFailure,
        reason: str,
    ) -> "WorktreeValidationResult":
        return cls(ok=False, reason=reason, failure=failure)


def load_completion_record(record_path: Path) -> CompletionRecord | None:
    """Read and validate a single completion record file.

    This is the ONE entry point for parsing an untrusted completion
    record: applies the per-file size gate BEFORE ``json.load`` runs,
    then delegates to ``CompletionRecord.from_dict`` for field-level
    bounds. All call sites (the publish-path validator, the observer
    that scans sessions for completions) must route through this
    function so an agent cannot bypass the gate by hitting a
    duplicate reader — that was the bug flagged in #6017 re-review-2
    P3. Returns ``None`` for any failure mode (missing file, oversized
    file, malformed JSON, invalid record); callers log context.
    """
    if not record_path.exists():
        logger.info("No completion record found at %s", record_path)
        return None

    try:
        size = record_path.stat().st_size
    except OSError as exc:
        logger.error("Could not stat completion record %s: %s", record_path, exc)
        return None
    if size > _MAX_COMPLETION_FILE_BYTES:
        logger.error(
            "Completion record %s is %d bytes, exceeds max %d",
            record_path,
            size,
            _MAX_COMPLETION_FILE_BYTES,
        )
        return None

    try:
        with open(record_path) as f:
            data = json.load(f)
        record = CompletionRecord.from_dict(data)
        logger.info(
            "Read completion record: outcome=%s session=%s path=%s",
            record.outcome.value,
            record.session_id,
            record_path,
        )
        return record
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in completion record %s: %s", record_path, exc)
        return None
    except ValueError as exc:
        logger.error("Invalid completion record %s: %s", record_path, exc)
        return None


class CompletionValidationGitAdapter(Protocol):
    def get_current_branch(self, worktree: Path) -> str | None: ...
    def has_uncommitted_changes(self, worktree: Path) -> bool: ...
    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool: ...

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        """Enumerate dirty file paths for the given mode.

        Return the enumerated paths on success, ``None`` when
        enumeration itself failed (git error, etc.). Callers MUST treat
        ``None`` as fail-closed; an empty list ``[]`` is a valid "all
        dirty entries were filtered" result that callers may pass.

        The boolean ``has_*_changes`` helpers in this protocol
        intentionally fail closed by returning ``True`` on error;
        ``list_dirty_files`` needs the same fail-closed semantics, but a
        bare ``list`` return type would collapse "filtered to empty" and
        "could not enumerate" into the same value — hence ``None``.
        """
        ...


class CompletionRecordValidator:
    """Loads completion records and validates publish preconditions."""

    def __init__(
        self,
        *,
        config: "Config | None",
        git_adapter: CompletionValidationGitAdapter,
    ) -> None:
        self._config = config
        self._git_adapter = git_adapter

    def read_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        """Read and validate a completion record from a worktree."""
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        return load_completion_record(record_path)

    def resolve_agent_label_from_completion_path(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        if completion_path is None or self._config is None:
            return None, None
        filename = Path(completion_path).name
        if not (filename.startswith("completion-") and filename.endswith(".json")):
            return None, None
        safe_name = filename[len("completion-"):-len(".json")]
        matches = [
            label
            for label in self._config.agents.keys()
            if sanitize_agent_label(label) == safe_name
        ]
        if not matches:
            return None, None
        if len(matches) > 1:
            return (
                None,
                "Multiple agent labels map to completion file "
                f"{filename}: {', '.join(matches)}",
            )
        return matches[0], None

    def validate_worktree_state(
        self, worktree: Path, record: CompletionRecord
    ) -> WorktreeValidationResult:
        """Validate worktree state before executing requested publish actions."""
        branch = self._git_adapter.get_current_branch(worktree)
        if not branch:
            return WorktreeValidationResult.fail(
                WorktreeValidationFailure.CURRENT_BRANCH_UNKNOWN,
                "Could not determine current branch",
            )

        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            if branch in ("main", "master"):
                return WorktreeValidationResult.fail(
                    WorktreeValidationFailure.PROTECTED_BRANCH,
                    f"Cannot push: on protected branch '{branch}'",
                )

            dirty_policy = self.check_dirty_policy(worktree)
            if not dirty_policy.ok:
                return dirty_policy

        return WorktreeValidationResult.pass_()

    def check_dirty_policy(self, worktree: Path) -> WorktreeValidationResult:
        """Apply validation.pre_push_dirty_check policy before push actions."""
        mode = (
            self._config.validation.pre_push_dirty_check
            if self._config is not None
            else "off"
        )

        if mode == "off":
            logger.info("Dirty-check skipped for %s: mode=off", worktree)
            return WorktreeValidationResult.pass_()
        list_mode = mode
        if mode == "tracked":
            dirty = self._git_adapter.has_tracked_changes(worktree, include_staged=True)
        elif mode == "unstaged":
            dirty = self._git_adapter.has_tracked_changes(worktree, include_staged=False)
        elif mode == "all":
            dirty = self._git_adapter.has_uncommitted_changes(worktree)
        else:
            return WorktreeValidationResult.fail(
                WorktreeValidationFailure.DIRTY_POLICY,
                (
                    "Invalid validation.pre_push_dirty_check value: "
                    f"{mode!r} (expected tracked|unstaged|all|off)"
                ),
            )

        logger.debug(
            "Dirty-check evaluated for %s: mode=%s dirty=%s",
            worktree,
            mode,
            dirty,
        )
        if dirty:
            dirty_files = self._git_adapter.list_dirty_files(worktree, list_mode)
            if dirty_files is None:
                # ``has_*_changes`` said the worktree is dirty, but the
                # enumeration call failed. Without the file list we
                # cannot tell whether the dirty state is the
                # planted/runtime-only kind that's safe to push or a
                # real blocking change. The boolean helpers fail closed
                # by returning ``True`` on git error; preserve that
                # invariant here by treating "unknown dirty state" as a
                # blocking failure rather than collapsing it to
                # "blocking_files == [] -> pass" (#6159).
                logger.warning(
                    "Dirty-check enumeration failed for %s (mode=%s); "
                    "failing closed",
                    worktree,
                    mode,
                )
                return WorktreeValidationResult.fail(
                    WorktreeValidationFailure.DIRTY_POLICY,
                    (
                        "Could not enumerate dirty files "
                        f"(validation.pre_push_dirty_check={mode!r}); "
                        "fail-closed because dirty state is unknown."
                    ),
                )
            blocking_files = filter_runtime_managed_dirty_paths(dirty_files, worktree)
            logger.info(
                "Dirty-check files for %s: mode=%s total=%d blocking=%d files=%s",
                worktree,
                mode,
                len(dirty_files),
                len(blocking_files),
                ", ".join(blocking_files[:_DIRTY_FILES_REASON_LIMIT])
                if blocking_files
                else "<runtime-only>",
            )
            if not blocking_files:
                # Bool short-circuit (has_uncommitted_changes / has_tracked_changes)
                # can fire on paths that ``list_dirty_files`` then filters out:
                # orchestrator-planted untracked files in mode=all (filtered
                # inside list_dirty_files) and runtime-managed metadata
                # (filtered here). Either way, ``blocking_files`` is the
                # authoritative gate — empty means nothing to block on.
                if dirty_files:
                    logger.info(
                        "Dirty-check ignored runtime-only files for %s: %s",
                        worktree,
                        ", ".join(dirty_files),
                    )
                else:
                    logger.info(
                        "Dirty-check found no blocking files for %s "
                        "(planted/runtime entries filtered)",
                        worktree,
                    )
                return WorktreeValidationResult.pass_()
            reason = (
                "Working tree is dirty; commit/add/stash before pushing. "
                "Override with validation.pre_push_dirty_check."
            )
            if blocking_files:
                preview = ", ".join(blocking_files[:_DIRTY_FILES_REASON_LIMIT])
                remaining = len(blocking_files) - _DIRTY_FILES_REASON_LIMIT
                suffix = f" (+{remaining} more)" if remaining > 0 else ""
                reason = f"{reason} Dirty files: {preview}{suffix}."
            return WorktreeValidationResult.fail(
                WorktreeValidationFailure.DIRTY_POLICY,
                reason,
            )

        return WorktreeValidationResult.pass_()
