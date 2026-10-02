"""How the anomalies moved between two engine audits (#7490).

An anomaly is identified by ``(kind, subject, signature)`` (``Anomaly.key``),
so the same stuck record, parked action or repeating log shape is the same
anomaly in both reports however its count or wording changed.

A no-progress anomaly (a repeat counted in the audit window) that is gone is
resolved only if its subject changed state after the previous audit; one that
merely aged out of the window is unobserved.

A previous anomaly missing from the current report is *resolved* only if the
current audit fully read every source it rests on. If one was absent, rate
limited or only partly read (a log tail that starts inside the window) this
time, nothing was observed about it, and it is reported as *unobserved*: a
rate-limited GitHub read must not turn every attention label into a fix.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..contracts.engine_audit import (
    ENGINE_AUDIT_SCHEMA_VERSION,
    Anomaly,
    AnomalyKind,
    AuditDiff,
    EngineAuditReport,
    PersistingAnomaly,
)


class IncomparableAuditError(ValueError):
    """The previous report cannot be compared with this one."""


def load_report(path: Path) -> EngineAuditReport:
    """A report written by an earlier audit, validated against the contract."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if version != ENGINE_AUDIT_SCHEMA_VERSION:
        raise IncomparableAuditError(
            f"{path} is engine-audit schema {version!r}, not {ENGINE_AUDIT_SCHEMA_VERSION}"
        )
    return EngineAuditReport.model_validate(payload)


def diff_reports(previous: EngineAuditReport, current: EngineAuditReport) -> AuditDiff:
    if previous.repo != current.repo:
        raise IncomparableAuditError(
            f"previous audit is of {previous.repo}, this one of {current.repo}"
        )
    before = {a.key: a for a in previous.anomalies}
    after = {a.key: a for a in current.anomalies}
    unread = current.unread_sources()
    gone = [a for key, a in before.items() if key not in after]
    observed = [a for a in gone if unread.isdisjoint(a.sources) and _absence_proves(a, previous, current)]
    return AuditDiff(
        previous_generated_at=previous.generated_at,
        new=tuple(a for key, a in after.items() if key not in before),
        resolved=tuple(observed),
        persisting=tuple(
            PersistingAnomaly(anomaly=a, previous_count=before[key].count)
            for key, a in after.items()
            if key in before
        ),
        unobserved=tuple(a for a in gone if a not in observed),
    )


#: Anomalies found by counting repeats inside the audit window. They can
#: vanish because the repeats aged out of the window, not because anything
#: was fixed.
_WINDOWED = frozenset(
    {AnomalyKind.NO_PROGRESS_LOG, AnomalyKind.NO_PROGRESS_TIMELINE, AnomalyKind.REFUSED_WORK}
)


def _absence_proves(anomaly: Anomaly, previous: EngineAuditReport, current: EngineAuditReport) -> bool:
    """Whether ``anomaly`` missing from ``current`` shows it resolved.

    A windowed anomaly is resolved only by its subject's state changing after
    the previous audit; every other anomaly is a present-tense fact of its
    fully-read source.
    """
    if anomaly.kind not in _WINDOWED:
        return True
    since = datetime.fromisoformat(previous.generated_at)
    return any(
        change.subject == anomaly.subject and datetime.fromisoformat(change.at) > since
        for change in current.no_progress.state_changes
    )


__all__ = ["IncomparableAuditError", "diff_reports", "load_report"]
