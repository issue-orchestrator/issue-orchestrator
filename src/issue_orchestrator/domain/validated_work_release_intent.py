"""The untrusted half of a validated-work release: what a tech lead proposes (#9092).

Kept free of the validated-work store types so the decision-artifact parser
can depend on it without an import cycle. The bound, approvable half is
:class:`~.validated_work_release.ValidatedWorkRelease`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

#: The tech-lead action kind that proposes a release.
RELEASE_VALIDATED_WORK_ACTION = "release_validated_work"

#: Records one release may name. A rewrite strands a handful of heads per
#: issue; a bound keeps the agent-authored list from becoming a bulk channel.
MAX_RELEASE_RECORDS = 20

_INTENT_KEYS = frozenset({"record_ids", "superseding_pr_number"})


def parse_record_ids(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{context} record_ids must be a list of record ids")
    ids = cast(tuple[object, ...], tuple(value))
    if not ids or len(ids) > MAX_RELEASE_RECORDS:
        raise ValueError(
            f"{context} must name between 1 and {MAX_RELEASE_RECORDS} records"
        )
    if any(not isinstance(item, str) or not item.strip() for item in ids):
        raise ValueError(f"{context} record_ids must be non-empty strings")
    if len(set(ids)) != len(ids):
        raise ValueError(f"{context} record_ids must be distinct")
    return cast(tuple[str, ...], ids)


def parse_pr_number(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{context} superseding_pr_number must be a positive integer,"
            f" got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ValidatedWorkReleaseIntent:
    """A tech lead's proposal to release named records. Untrusted until bound."""

    record_ids: tuple[str, ...]
    #: The merged PR of the same issue that rebuilt the records' work.
    superseding_pr_number: int

    def __post_init__(self) -> None:
        parse_record_ids(self.record_ids, context="release")
        if type(self.record_ids) is not tuple:
            raise ValueError("release record_ids must be a tuple")
        parse_pr_number(self.superseding_pr_number, context="release")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_ids": list(self.record_ids),
            "superseding_pr_number": self.superseding_pr_number,
        }

    @classmethod
    def from_mapping(cls, data: object, *, context: str) -> "ValidatedWorkReleaseIntent":
        """Parse the agent's ``release`` object; malformed input raises."""
        if not isinstance(data, Mapping):
            raise ValueError(f"{context} release must be an object")
        raw = cast(Mapping[str, object], data)
        unexpected = sorted(set(raw) - _INTENT_KEYS)
        if unexpected:
            raise ValueError(f"{context} release has unexpected fields: {unexpected}")
        return cls(
            record_ids=parse_record_ids(raw.get("record_ids"), context=context),
            superseding_pr_number=parse_pr_number(
                raw.get("superseding_pr_number"), context=context
            ),
        )


