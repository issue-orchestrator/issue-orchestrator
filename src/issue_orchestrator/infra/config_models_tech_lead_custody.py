"""The ``tech_lead.custody`` config block (#7331): when a custody state is stale.

```yaml
tech_lead:
  custody:
    stale_after_minutes:
      queued_for_tech_lead: 240
      investigating: 120
      waiting_on_you: 1440
      being_fixed: 720
      waiting_on_world: 360
      held: 4320
      verify: 1440
```

Every blocked item has a custody state (see
:mod:`..domain.blocked_item_custody`). An item that sits in an OWNED state
longer than that state's threshold is stale, and the board's attention count
includes it next to every unowned item. Unowned has no threshold: it needs
attention at any age.

Fail-fast by shape: an unknown state, a non-integer, or a threshold below one
minute is a configuration error, never a silent default. A state left out keeps
its default, so the settings form can write one threshold at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Any

from ..domain.blocked_item_custody import (
    OWNED_CUSTODY_STATES,
    CustodyStaleThresholds,
    CustodyState,
)

#: Defaults, in minutes. Each is the longest a healthy item plausibly stays in
#: the state: a queued investigation should launch within a stuck-sweep
#: interval, a running one should end within two hours, and a remedy should
#: show its effect within a day.
DEFAULT_CUSTODY_STALE_MINUTES: dict[CustodyState, int] = {
    CustodyState.QUEUED_FOR_TECH_LEAD: 240,
    CustodyState.INVESTIGATING: 120,
    CustodyState.WAITING_ON_YOU: 1440,
    CustodyState.BEING_FIXED: 720,
    CustodyState.WAITING_ON_WORLD: 360,
    CustodyState.HELD: 4320,
    CustodyState.VERIFY: 1440,
}

#: The largest accepted threshold: thirty days. Beyond that "stale" means
#: nothing on a board that moves daily.
MAX_CUSTODY_STALE_MINUTES = 30 * 24 * 60


def _minutes(state: CustodyState) -> Any:
    return field(default=DEFAULT_CUSTODY_STALE_MINUTES[state])


@dataclass
class CustodyStaleAfterMinutes:
    """One threshold per owned custody state; attribute name = state value."""

    queued_for_tech_lead: int = _minutes(CustodyState.QUEUED_FOR_TECH_LEAD)
    investigating: int = _minutes(CustodyState.INVESTIGATING)
    waiting_on_you: int = _minutes(CustodyState.WAITING_ON_YOU)
    being_fixed: int = _minutes(CustodyState.BEING_FIXED)
    waiting_on_world: int = _minutes(CustodyState.WAITING_ON_WORLD)
    held: int = _minutes(CustodyState.HELD)
    verify: int = _minutes(CustodyState.VERIFY)

    def __post_init__(self) -> None:
        names = {f.name for f in fields(self)}
        expected = {state.value for state in OWNED_CUSTODY_STATES}
        if names != expected:
            raise ValueError(
                "tech_lead.custody.stale_after_minutes must name every owned custody"
                f" state exactly: {sorted(expected)}, has {sorted(names)}"
            )

    @classmethod
    def from_mapping(cls, data: Any) -> "CustodyStaleAfterMinutes":
        path = "tech_lead.custody.stale_after_minutes"
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(
                f"{path} must be a mapping of custody state to minutes, got"
                f" {type(data).__name__} ({data!r})"
            )
        known = {state.value for state in OWNED_CUSTODY_STATES}
        unknown = sorted(str(key) for key in set(data) - known)
        if unknown:
            raise ValueError(
                f"{path} has unknown state(s) {', '.join(unknown)}; supported"
                f" states are {', '.join(sorted(known))}"
            )
        for key, value in data.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{path}.{key} must be a whole number of minutes, got"
                    f" {type(value).__name__} ({value!r})"
                )
        return cls(**data)

    def errors(self) -> list[str]:
        return [
            f"tech_lead.custody.stale_after_minutes.{state.value} must be between 1"
            f" and {MAX_CUSTODY_STALE_MINUTES} minutes, got {minutes}"
            for state in OWNED_CUSTODY_STATES
            for minutes in [getattr(self, state.value)]
            if not 1 <= minutes <= MAX_CUSTODY_STALE_MINUTES
        ]

    def to_thresholds(self) -> CustodyStaleThresholds:
        errors = self.errors()
        if errors:
            raise ValueError("; ".join(errors))
        return CustodyStaleThresholds(
            by_state={
                state: timedelta(minutes=getattr(self, state.value))
                for state in OWNED_CUSTODY_STATES
            }
        )

    def to_event_dict(self) -> dict[str, int]:
        return {state.value: getattr(self, state.value) for state in OWNED_CUSTODY_STATES}


@dataclass
class TechLeadCustodyConfig:
    """Custody settings for the board's blocked lane (#7331)."""

    stale_after_minutes: CustodyStaleAfterMinutes = field(
        default_factory=CustodyStaleAfterMinutes
    )

    @classmethod
    def from_mapping(cls, data: Any) -> "TechLeadCustodyConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(
                "tech_lead.custody must be a mapping, got"
                f" {type(data).__name__} ({data!r})"
            )
        unknown = sorted(str(key) for key in set(data) - {"stale_after_minutes"})
        if unknown:
            raise ValueError(
                f"tech_lead.custody has unknown key(s) {', '.join(unknown)};"
                " supported keys are stale_after_minutes"
            )
        return cls(
            stale_after_minutes=CustodyStaleAfterMinutes.from_mapping(
                data.get("stale_after_minutes")
            )
        )

    def startup_errors(self) -> list[str]:
        return self.stale_after_minutes.errors()

    def to_event_dict(self) -> dict[str, object]:
        return {"stale_after_minutes": self.stale_after_minutes.to_event_dict()}


__all__ = [
    "DEFAULT_CUSTODY_STALE_MINUTES",
    "MAX_CUSTODY_STALE_MINUTES",
    "CustodyStaleAfterMinutes",
    "TechLeadCustodyConfig",
]
