"""File-backed internal-review instructions for coder prompt composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.coder_prompt import (
    CoderPromptAddendumPreparation,
    CoderPromptAddendumUnavailable,
    PreparedCoderPromptAddendum,
    build_internal_review_addendum,
)
from ..domain.session_kind import CompletionProtocol, SessionKind
from ..ports.coder_prompt import CoderPromptAddendumProvider

if TYPE_CHECKING:
    from ..infra.config import Config


@dataclass(frozen=True, slots=True)
class FileInternalReviewPromptAddendum:
    """Load trusted repository instructions and render the coder-side contract."""

    repository_root: Path
    enabled: bool
    max_rounds: int
    instructions_path: str

    def prepare(self, *, kind: SessionKind) -> CoderPromptAddendumPreparation:
        """Resolve the addendum once, centrally excluding all non-coder kinds."""
        if not self._applies_to(kind):
            return PreparedCoderPromptAddendum(None)
        try:
            instructions_path = self._contained_instructions_path()
            instructions = instructions_path.read_text(encoding="utf-8").strip()
            if not instructions:
                raise ValueError(
                    "review.internal.instructions must reference a non-empty file: "
                    f"{instructions_path}"
                )
            addendum = build_internal_review_addendum(
                instructions=instructions,
                max_rounds=self.max_rounds,
                source=self.instructions_path,
            )
        except (OSError, ValueError) as exc:
            return CoderPromptAddendumUnavailable(str(exc))
        return PreparedCoderPromptAddendum(addendum)

    def _applies_to(self, kind: SessionKind) -> bool:
        """Own the complete internal-review policy in one place.

        It reviews the issue's DELIVERABLE before an agent reports it, so it
        applies to the agent kinds whose coding-done completion is captured as
        the issue's work: coding and rework. A tech-lead run's publication is
        not the issue's work (#7347), and a historical import has no agent.
        """
        capabilities = kind.capabilities
        return (
            self.enabled
            and capabilities.capturable
            and capabilities.completion_protocol is CompletionProtocol.CODING_DONE
        )

    def _contained_instructions_path(self) -> Path:
        """Resolve instructions from the trusted, non-mutating repository root."""
        repository_root = self.repository_root.resolve()
        configured = Path(self.instructions_path)
        if configured.is_absolute():
            raise ValueError("review.internal.instructions must be repository-relative")
        candidate = (repository_root / configured).resolve()
        try:
            candidate.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError(
                "review.internal.instructions must stay inside the repository root"
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(
                "review.internal.instructions file not found in repository root: "
                f"{candidate}"
            )
        return candidate


def build_coder_prompt_addendum_provider(
    config: "Config",
) -> CoderPromptAddendumProvider:
    """Build the process-scoped provider from validated runtime configuration."""
    return FileInternalReviewPromptAddendum(
        repository_root=config.repo_root,
        enabled=config.internal_review_enabled,
        max_rounds=config.internal_review_max_rounds,
        instructions_path=config.internal_review_instructions,
    )
