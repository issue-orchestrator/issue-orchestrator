"""Fresh lifecycle rerun prompt context."""

from __future__ import annotations

from collections.abc import Mapping

FRESH_LIFECYCLE_RERUN_INTENT = "fresh_lifecycle"

_SHARED_CONTEXT = (
    "This issue is being rerun to get a fresh coding, validation, and review "
    "cycle under the current repository state, current orchestrator behavior, "
    "and current coding/review prompts."
)


def coder_fresh_lifecycle_rerun_context() -> str:
    """Return the standard fresh-rerun context for coding turns."""
    return (
        "Fresh lifecycle rerun:\n"
        f"{_SHARED_CONTEXT} Treat the issue as active work. Reassess the "
        "implementation against the issue requirements and current codebase, "
        "make any needed changes, run validation, and complete normally. If "
        "no code changes are needed, record the verification performed and "
        "why the current implementation still satisfies the issue, so the "
        "reviewer can perform a fresh review."
    )


def reviewer_fresh_lifecycle_rerun_context() -> str:
    """Return the standard fresh-rerun context for review turns."""
    return (
        "Fresh lifecycle rerun:\n"
        f"{_SHARED_CONTEXT} Perform a fresh review even if the diff is small "
        "or unchanged from a prior PR. Review the issue requirements, current "
        "worktree, coder completion rationale, validation evidence, and "
        "relevant current codebase context. Do not answer that there is "
        "nothing to review solely because prior work existed."
    )


def prepend_fresh_lifecycle_rerun_context(prompt: str) -> str:
    """Prepend the standard coding rerun context to an initial prompt."""
    return f"{coder_fresh_lifecycle_rerun_context()}\n\n{prompt}"


def manifest_has_fresh_lifecycle_rerun(
    manifest: Mapping[str, object] | None,
) -> bool:
    """Return whether a session manifest carries fresh lifecycle rerun intent."""
    return bool(manifest and manifest.get("rerun_intent") == FRESH_LIFECYCLE_RERUN_INTENT)
