"""One precise timestamp interpretation for receipt validation and evidence."""

import re
from datetime import datetime, timezone


# The persisted shape is datetime.isoformat(): a full date AND time, optionally
# with fractional seconds and a UTC offset. Also accept the equivalent Z and
# space separator understood by the prior display reader. fromisoformat validates the
# calendar/offset values after this guard excludes its date-only shorthand.
_RECEIPT_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]{1,6})?)?)?"
)


def parse_receipt_time(value: object) -> datetime:
    """Decode a present receipt timestamp; absence is the caller's decision."""
    if not isinstance(value, str) or _RECEIPT_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Receipt timestamps require a full ISO date and time")
    return datetime.fromisoformat(value)


def receipt_time(value: datetime) -> datetime:
    """Normalize offsets to UTC and preserve legacy naive wall timestamps."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
