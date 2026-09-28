"""How the anomalies moved between two engine audits (#7490).

An anomaly is identified by ``(kind, subject, signature)`` (``Anomaly.key``),
so the same stuck record, parked action or repeating log shape is the same
anomaly in both reports however its count or wording changed.

A previous anomaly missing from the current report is *resolved* only if the
current audit read its source. If that source was absent or rate limited this
time, nothing was observed about it, and it is reported as *unobserved*: a
rate-limited GitHub read must not turn every attention label into a fix.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..contracts.engine_audit import (
    ENGINE_AUDIT_SCHEMA_VERSION,
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
    return AuditDiff(
        previous_generated_at=previous.generated_at,
        new=tuple(a for key, a in after.items() if key not in before),
        resolved=tuple(a for a in gone if a.source not in unread),
        persisting=tuple(
            PersistingAnomaly(anomaly=a, previous_count=before[key].count)
            for key, a in after.items()
            if key in before
        ),
        unobserved=tuple(a for a in gone if a.source in unread),
    )


__all__ = ["IncomparableAuditError", "diff_reports", "load_report"]
