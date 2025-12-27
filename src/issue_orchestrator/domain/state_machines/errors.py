"""Domain-level state machine errors.

These errors are part of the domain contract and must not leak implementation
details (e.g., transitions.MachineError) to control/adapters.

- InvalidStateTransition: a requested trigger is not valid from the current state.
- StateInvariantViolation: detected an impossible/corrupted state (drift/corruption).
"""

from __future__ import annotations


class StateMachineError(Exception):
    """Base class for domain-level state machine errors."""


class InvalidStateTransition(StateMachineError):
    """A requested trigger is not allowed from the current state."""


class StateInvariantViolation(StateMachineError):
    """Detected an impossible or corrupted state."""
