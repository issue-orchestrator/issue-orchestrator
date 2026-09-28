"""Behavior-level port for repository-owned coder prompt addenda."""

from __future__ import annotations

from typing import Protocol

from ..domain.coder_prompt import (
    CoderPromptAddendumPreparation,
    PreparedCoderPromptAddendum,
)
from ..domain.session_kind import SessionKind


class CoderPromptAddendumProvider(Protocol):
    """Prepare the optional instructions for one kind-aware prompt."""

    def prepare(self, *, kind: SessionKind) -> CoderPromptAddendumPreparation:
        """Resolve trusted addendum I/O before the caller mutates launch state."""
        ...


class NoCoderPromptAddendum:
    """Explicit null implementation used when no coder addendum is configured."""

    def prepare(self, *, kind: SessionKind) -> PreparedCoderPromptAddendum:
        _ = kind
        return PreparedCoderPromptAddendum(None)


NO_CODER_PROMPT_ADDENDUM = NoCoderPromptAddendum()
