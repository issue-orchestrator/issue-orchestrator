"""Framework resources - canonical docs injected into agent sessions."""

from importlib import resources
from functools import lru_cache


@lru_cache(maxsize=1)
def get_agent_done_instructions() -> str:
    """Load the canonical agent-done instructions.

    These are injected into every agent session's system prompt
    so agents always know how to signal completion.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("agent_done.md").read_text()
