"""Provider circuit breaker ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class ProviderErrorType(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    FATAL = "fatal"


@dataclass(frozen=True)
class ProviderCircuitState:
    provider: str
    open_until: datetime | None
    consecutive_outages: int
    last_error_summary: str | None
    updated_at: datetime


class ProviderCircuitStore(Protocol):
    """Persistence for provider circuit breaker state."""

    def get(self, provider: str) -> ProviderCircuitState | None:
        ...

    def list_all(self) -> list[ProviderCircuitState]:
        ...

    def save(self, state: ProviderCircuitState) -> None:
        ...

    def delete(self, provider: str) -> None:
        ...


class InMemoryProviderCircuitStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._states: dict[str, ProviderCircuitState] = {}

    def get(self, provider: str) -> ProviderCircuitState | None:
        return self._states.get(provider)

    def list_all(self) -> list[ProviderCircuitState]:
        return list(self._states.values())

    def save(self, state: ProviderCircuitState) -> None:
        self._states[state.provider] = state

    def delete(self, provider: str) -> None:
        self._states.pop(provider, None)
