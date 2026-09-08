"""Receipt-based delivery monitoring, independent of the displayed run history.

A completed post-apply receipt (including a proposal awaiting approval or a
valid no-op) and a legitimate human escalation are successful delivery. This
monitor never uses action counts or agent intent as a substitute for receipts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .tech_lead_receipt_time import receipt_time as delivery_time


class DeliveryHistoryState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TechLeadDeliveryEvidence:
    history_state: DeliveryHistoryState = DeliveryHistoryState.COMPLETE
    last_delivered_at: datetime | None = None
    first_undelivered_at: datetime | None = None
    latest_started_at: datetime | None = None
    runs_without_delivery: int = 0


class TechLeadDeliveryStatus(str, Enum):
    OBSERVING = "observing"
    STALLED = "stalled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TechLeadDeliveryPolicy:
    silence: timedelta = timedelta(hours=6)
    minimum_runs: int = 3

    def __post_init__(self) -> None:
        if self.silence <= timedelta(0) or self.minimum_runs < 2:
            raise ValueError(
                "Delivery monitoring requires positive silence and repeated runs"
            )

    def evaluate(
        self, evidence: TechLeadDeliveryEvidence, *, now: datetime
    ) -> TechLeadDeliveryStatus:
        if evidence.history_state is not DeliveryHistoryState.COMPLETE:
            return TechLeadDeliveryStatus.UNKNOWN
        latest = evidence.latest_started_at
        baseline = evidence.last_delivered_at or evidence.first_undelivered_at
        if latest is None or baseline is None:
            return TechLeadDeliveryStatus.OBSERVING
        now = delivery_time(now)
        if (
            evidence.runs_without_delivery >= self.minimum_runs
            and now - delivery_time(baseline) >= self.silence
            and timedelta(0) <= now - delivery_time(latest) <= self.silence
        ):
            return TechLeadDeliveryStatus.STALLED
        return TechLeadDeliveryStatus.OBSERVING


DEFAULT_TECH_LEAD_DELIVERY_POLICY = TechLeadDeliveryPolicy()
