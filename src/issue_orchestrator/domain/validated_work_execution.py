"""Opaque local execution capabilities, separate from durable claim secrets."""

from dataclasses import dataclass
from typing import Never

from .validated_work import require_text


@dataclass(frozen=True, slots=True)
class RecordExecutionBusy:
    record_id: str

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")


class RecordExecutionBusyError(RuntimeError):
    def __init__(self, record_id: str) -> None:
        require_text(record_id, "record_id")
        self.record_id = record_id
        super().__init__("Validated-work execution is busy")


class RecordExecutionToken:
    """Owner-minted identity capability; never construct, copy or serialize."""

    __slots__ = ()

    def __new__(cls) -> Never:
        raise TypeError("execution tokens are minted by the execution owner")

    def __reduce__(self) -> Never:
        raise TypeError("execution tokens cannot be serialized or copied")
