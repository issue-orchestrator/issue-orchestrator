"""Observe every in-scope engine's activity from byte copies of its state (#7567).

The composition the improver's trigger reads: for each engine the inventory
names, the same snapshots ``io engine-audit`` takes (:mod:`.engine_snapshot`,
never a live database), one local audit (GitHub is never read: a trigger
check spends no API budget), and its watermark
(:func:`~..observation.engine_activity.engine_activity`).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..contracts.engine_audit import SourceStatus
from ..domain.engine_activity import EngineActivityObservation
from ..observation.engine_activity import engine_activity
from ..observation.engine_audit import Unavailable, audit_engine
from ..ports.engine_activity import EngineInventory
from .engine_snapshot import snapshot_engine

#: The audit window the anomalies are found over; the same each time, so two
#: observations' anomaly sets compare.
ACTIVITY_AUDIT_WINDOW = timedelta(hours=24)
#: How much of each engine's log the probe reads.
ACTIVITY_LOG_TAIL_BYTES = 16 * 1024 * 1024


class SnapshotEngineActivityProbe:
    def __init__(self, inventory: EngineInventory) -> None:
        self._inventory = inventory

    def observe(self, *, now: datetime, since: datetime) -> EngineActivityObservation:
        engines = []
        for sighting in self._inventory.engines(since=since):
            engine = sighting.engine
            with tempfile.TemporaryDirectory(prefix="io-engine-activity-") as scratch:
                snapshot = snapshot_engine(
                    engine.state_dir,
                    Path(scratch),
                    repo=engine.repo,
                    log_tail_bytes=ACTIVITY_LOG_TAIL_BYTES,
                    github=Unavailable(SourceStatus.SKIPPED, "the activity probe reads local state only"),
                )
                report = audit_engine(snapshot.audit, now=now, window=ACTIVITY_AUDIT_WINDOW)
            engines.append(engine_activity(engine, report))
        return EngineActivityObservation(observed_at=now, engines=tuple(engines))


__all__ = ["ACTIVITY_AUDIT_WINDOW", "ACTIVITY_LOG_TAIL_BYTES", "SnapshotEngineActivityProbe"]
