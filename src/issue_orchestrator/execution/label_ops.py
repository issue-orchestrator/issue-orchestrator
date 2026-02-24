"""Shared label operation helpers for API/control surfaces."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol


class _LabelMutable(Protocol):
    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...


@dataclass(frozen=True)
class LabelOperation:
    operation: str
    number: int
    label: str


def apply_label_operations(
    label_target: _LabelMutable,
    operations: list[LabelOperation],
    *,
    logger: logging.Logger,
    log_prefix: str,
) -> None:
    """Apply label operations in order, logging and continuing on failures."""
    for op in operations:
        try:
            if op.operation == "add":
                label_target.add_label(op.number, op.label)
            elif op.operation == "remove":
                label_target.remove_label(op.number, op.label)
        except Exception as exc:
            logger.warning(
                "%s label %s failed (%s #%d): %s",
                log_prefix,
                op.operation,
                op.label,
                op.number,
                exc,
            )
