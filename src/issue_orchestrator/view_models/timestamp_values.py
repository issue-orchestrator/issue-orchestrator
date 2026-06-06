"""Timestamp value normalization for dashboard-facing view models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

DETAIL_VALUE_KINDS_KEY = "detail_value_kinds"
TIMESTAMP_VALUE_KIND = "timestamp"


def dashboard_timestamp_source(value: object) -> str:
    if not isinstance(value, datetime):
        return str(value)
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def timeline_detail_value_kinds(event: Mapping[str, object]) -> Mapping[str, str]:
    return {
        key: TIMESTAMP_VALUE_KIND
        for key, value in event.items()
        if key not in {"actions", DETAIL_VALUE_KINDS_KEY}
        and _is_timezone_aware_timestamp_literal(value)
    }


def _is_timezone_aware_timestamp_literal(value: object) -> bool:
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if not raw or ("T" not in raw and " " not in raw):
        return False
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
