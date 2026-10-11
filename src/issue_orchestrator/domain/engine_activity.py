"""Engine activity: what makes a behaviour-graded suite due (#7567).

The budgeted code-change cadence (:class:`.budgeted_validation.ValidationCadence`)
answers "has io's code changed enough to test it again?". The tech-lead
improver grades the tech lead's BEHAVIOUR, which changes when the engines it
runs in do something, not when io merges. This module is that second
question's one owner:

* :class:`EngineRef` names one Repository Engine: its Control Center key, the
  repository it works and the state directory it writes;
* :class:`EngineActivity` is one engine's activity watermark: how many
  tech-lead decisions and completions it has recorded, and which audit
  anomalies it currently shows;
* :class:`EngineActivityObservation` is every in-scope engine's watermark at
  one instant; :meth:`~EngineActivityObservation.active_since` says which
  engines did something since an earlier observation;
* :class:`EngineActivityCadence` decides, from the last attempt, the last
  probe and the activity, whether the suite is due: at most once per
  ``max_delay_hours``, and only when some engine has new activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

ENGINE_ACTIVITY_KIND = "engine_activity"


@dataclass(frozen=True, slots=True)
class EngineRef:
    """One Repository Engine, as Control Center registers it."""

    #: The Control Center repository key (``repo-<sha256>``) of the engine's root.
    engine_id: str
    #: ``owner/repo`` the engine works.
    repo: str
    #: The engine's state directory (read only, through byte copies).
    state_dir: Path

    def __post_init__(self) -> None:
        if not self.engine_id or not self.repo:
            raise ValueError("an engine needs an id and a repository")

    @property
    def checkout(self) -> Path:
        """The engine's repository checkout (``<checkout>/.issue-orchestrator/state``)."""
        if self.state_dir.parent.name != ".issue-orchestrator" or self.state_dir.name != "state":
            raise ValueError(f"{self.state_dir} is not <checkout>/.issue-orchestrator/state")
        return self.state_dir.parent.parent


@dataclass(frozen=True, slots=True)
class EngineSighting:
    """An engine as Control Center sees it: whether it runs, and when it last
    wrote its log (every loop iteration logs, so nothing it records can
    postdate its last log write)."""

    engine: EngineRef
    running: bool
    last_written: datetime | None

    def __post_init__(self) -> None:
        _require_aware(self.last_written)

    def active_since(self, since: datetime) -> bool:
        """Whether it may have done anything at or after ``since``."""
        return ran_since(running=self.running, last_written=self.last_written, since=since)


@dataclass(frozen=True, slots=True)
class UnidentifiedEngine:
    """A registered engine in scope whose identity cannot be read (no start
    record from this version: it must restart). It is neither observed nor
    audited, and says why."""

    state_dir: Path
    reason: str


@dataclass(frozen=True, slots=True)
class EngineInventoryRead:
    sightings: tuple[EngineSighting, ...]
    unidentified: tuple[UnidentifiedEngine, ...] = ()


def ran_since(*, running: bool, last_written: datetime | None, since: datetime) -> bool:
    """An engine runs now, or last wrote its log at or after ``since``."""
    _require_aware(last_written, since)
    return running or (last_written is not None and last_written >= since)


@dataclass(frozen=True, slots=True)
class EngineActivity:
    """One engine's activity watermark.

    ``decisions`` and ``completions`` are totals of never-pruned ledgers
    (charter decisions, validated work records); None is a ledger the probe
    could not read. ``anomalies`` are the audit anomaly keys
    (``kind|subject|signature``) seen from the engine's local state.
    """

    engine_id: str
    repo: str
    decisions: int | None
    completions: int | None
    anomalies: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("decisions", "completions"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if tuple(sorted(set(self.anomalies))) != self.anomalies:
            raise ValueError("anomalies must be sorted and distinct")

    def new_since(self, earlier: EngineActivity | None) -> tuple[str, ...]:
        """What this watermark shows that ``earlier`` (the same engine's) did not.

        A ledger total that grew is new activity; a ledger read now but not
        then counts from zero. A total that shrank or was not read now shows
        nothing. Any anomaly key ``earlier`` lacked is new.
        """
        if earlier is not None and earlier.engine_id != self.engine_id:
            raise ValueError("activity compares one engine with itself")
        reasons = []
        for name in ("decisions", "completions"):
            now = getattr(self, name)
            before = None if earlier is None else getattr(earlier, name)
            if now is not None and now > (before or 0):
                reasons.append(f"{now - (before or 0)} new {name}")
        known = set() if earlier is None else set(earlier.anomalies)
        fresh = [key for key in self.anomalies if key not in known]
        if fresh:
            reasons.append(f"{len(fresh)} new anomalies")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class EngineActivityObservation:
    """Every in-scope engine's watermark, observed at ``observed_at``."""

    observed_at: datetime
    engines: tuple[EngineActivity, ...]
    #: In-scope engines whose identity could not be read (their state
    #: directories): their activity is unknown, never "none".
    unidentified: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("an activity observation must be timezone-aware")
        if tuple(sorted(set(self.unidentified))) != self.unidentified:
            raise ValueError("unidentified engines must be sorted and distinct")
        ids = [engine.engine_id for engine in self.engines]
        if len(ids) != len(set(ids)):
            raise ValueError("an observation holds each engine once")

    def engine(self, engine_id: str) -> EngineActivity | None:
        return next((e for e in self.engines if e.engine_id == engine_id), None)

    def active_since(self, baseline: EngineActivityObservation | None) -> dict[str, tuple[str, ...]]:
        """Each engine with activity ``baseline`` does not show, and what it is.

        No baseline (the suite never succeeded) counts every engine from zero.
        """
        active = {}
        for engine in self.engines:
            reasons = engine.new_since(None if baseline is None else baseline.engine(engine.engine_id))
            if reasons:
                active[engine.engine_id] = reasons
        return active


@dataclass(frozen=True, slots=True)
class EngineActivityCadence:
    """Due when an engine has new activity, at most once per ``max_delay_hours``.

    ``probe_interval_minutes`` bounds how often an open window re-observes the
    engines while none is active: an observation byte-copies every engine's
    stores, so it is not repeated on every scheduler check.
    """

    max_delay_hours: int = 24
    probe_interval_minutes: int = 60
    kind: Literal["engine_activity"] = field(default=ENGINE_ACTIVITY_KIND, init=False)

    def __post_init__(self) -> None:
        if type(self.max_delay_hours) is not int or self.max_delay_hours <= 0:
            raise ValueError("max_delay_hours must be a positive integer")
        if type(self.probe_interval_minutes) is not int or self.probe_interval_minutes <= 0:
            raise ValueError("probe_interval_minutes must be a positive integer")

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.max_delay_hours)

    def observe_since(self, *, now: datetime, baseline: EngineActivityObservation | None) -> datetime:
        """How far back an engine must have run to be observed: since the last
        successful run's watermark (an engine that did something after it and
        then stopped must still count), and at least the window."""
        _require_aware(now)
        floor = now - self.window
        return floor if baseline is None else min(floor, baseline.observed_at)

    def should_observe(
        self, *, now: datetime, last_attempt_at: datetime | None,
        last_probe: EngineActivityObservation | None,
    ) -> bool:
        """Whether to look at the engines now: the window since the last
        attempt is over, and no probe looked within the probe interval."""
        _require_aware(now, last_attempt_at)
        if last_attempt_at is not None and now < last_attempt_at + self.window:
            return False
        if last_probe is None:
            return True
        probed_at = last_probe.observed_at
        if last_attempt_at is not None and probed_at <= last_attempt_at:
            return True
        return now >= probed_at + timedelta(minutes=self.probe_interval_minutes)

    def due(
        self, *, observation: EngineActivityObservation,
        baseline: EngineActivityObservation | None,
    ) -> bool:
        """Some engine did something since the last successful run's
        watermark, or an engine in scope cannot be read at all: a run then
        reports it (unavailable) rather than the improver staying silent."""
        return bool(observation.active_since(baseline)) or bool(observation.unidentified)


def _require_aware(*instants: datetime | None) -> None:
    if any(i is not None and i.tzinfo is None for i in instants):
        raise ValueError("cadence timestamps must include a timezone")


__all__ = [
    "ENGINE_ACTIVITY_KIND",
    "EngineActivity",
    "EngineActivityCadence",
    "EngineActivityObservation",
    "EngineRef",
    "EngineInventoryRead",
    "EngineSighting",
    "UnidentifiedEngine",
    "ran_since",
]
