"""Read-only presentation facts for retained-work owner engines."""

from typing import Protocol

from ..domain.control_center_recovery import EnginePresentationFact
from ..domain.repository_engine_lifecycle import EngineIdentity


class RecoveryEnginePresentationReader(Protocol):
    def presentation_for(self, engine: EngineIdentity) -> EnginePresentationFact:
        """Describe this exact incarnation without changing ownership authority."""
        ...
