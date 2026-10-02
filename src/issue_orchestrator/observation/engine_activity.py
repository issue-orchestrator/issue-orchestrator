"""An engine's activity watermark, read off its audit (#7567).

The watermark is what the improver's trigger compares between runs:

* ``decisions``: every charter decision the tech lead has recorded (the
  charter ledger is never pruned), from ``tech_lead.charter_by_role_outcome``;
* ``completions``: every validated work record, one per agent completion that
  produced work, from ``validated_work.by_state_resolution``;
* ``anomalies``: the keys of the anomalies the audit found.

A section the audit could not read is None (unobserved), never zero.
"""

from __future__ import annotations

from ..contracts.engine_audit import Anomaly, EngineAuditReport
from ..domain.engine_activity import EngineActivity, EngineRef


def anomaly_token(anomaly: Anomaly) -> str:
    return "|".join(anomaly.key)


def engine_activity(engine: EngineRef, report: EngineAuditReport) -> EngineActivity:
    if report.repo != engine.repo:
        raise ValueError(f"an audit of {report.repo} is not engine {engine.engine_id}'s ({engine.repo})")
    tech_lead, work = report.tech_lead, report.validated_work
    return EngineActivity(
        engine_id=engine.engine_id,
        repo=engine.repo,
        decisions=None if tech_lead is None else sum(c.count for c in tech_lead.charter_by_role_outcome),
        completions=None if work is None else sum(c.count for c in work.by_state_resolution),
        anomalies=tuple(sorted({anomaly_token(a) for a in report.anomalies})),
    )


__all__ = ["anomaly_token", "engine_activity"]
