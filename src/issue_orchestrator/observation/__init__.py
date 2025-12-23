"""Observation layer - fact-gathering without authority.

This package contains components that observe and report facts.
These are the "Observers" in the architecture.

Architecture principle:
- Components that OBSERVE are named Observers (observation/)
- Components that DECIDE are named Controllers (control/)
- Components that ACT are named Adapters (execution/)

The observation layer:
- Gathers facts about the current state
- Reports what IS, not what SHOULD BE
- Does NOT make policy decisions
- Does NOT mutate state
"""

from .observer import SessionObserver
from .observation import SessionObservation, SessionObservationResult

# Backwards compatibility
SessionMonitor = SessionObserver

__all__ = [
    "SessionObserver",
    "SessionMonitor",  # Deprecated alias
    "SessionObservation",
    "SessionObservationResult",
]
