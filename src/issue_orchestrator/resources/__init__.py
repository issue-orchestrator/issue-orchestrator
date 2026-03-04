"""Framework resources - canonical docs injected into agent sessions."""

from importlib import resources
from functools import lru_cache


@lru_cache(maxsize=1)
def get_coding_done_instructions() -> str:
    """Load the canonical coding-done instructions.

    Injected into coding/rework agent sessions so they know
    how to signal completion with dirty-file checks and validation.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("coding_done.md").read_text()


@lru_cache(maxsize=1)
def get_reviewer_done_instructions() -> str:
    """Load the canonical reviewer-done instructions.

    Injected into review/triage agent sessions so they know
    how to signal their verdict.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("reviewer_done.md").read_text()


def get_completion_instructions(task_kind: str) -> str:
    """Load task-specific completion instructions.

    Args:
        task_kind: The task kind value (e.g., "code", "rework", "review", "triage").

    Returns:
        Markdown instructions for the appropriate completion command.
    """
    if task_kind in ("code", "rework"):
        return get_coding_done_instructions()
    return get_reviewer_done_instructions()
