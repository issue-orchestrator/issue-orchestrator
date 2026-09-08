"""Retention compares validated instants, never database text ordering."""

from datetime import datetime, timezone


def retention_instant(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("retention timestamp must be canonical aware ISO text")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.isoformat() != value:
        raise ValueError("retention timestamp must be canonical aware ISO text")
    return instant.astimezone(timezone.utc)
