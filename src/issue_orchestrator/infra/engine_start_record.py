"""Where an engine keeps the record of its latest start, and how it is read (#7490).

The engine writes :class:`~..contracts.engine_start.EngineStartRecord` once
per start into its state directory; a later start replaces it. Readers get
the record validated against its contract, or a typed refusal: an engine
that started on a version that did not write it has no record, and saying so
beats guessing a start time from a lock file or a rotated log.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..contracts.engine_start import EngineStartRecord
from .atomic_io import atomic_write_bytes

ENGINE_START_FILENAME = "engine-start.json"


class EngineStartRecordUnavailable(RuntimeError):
    """The state directory holds no readable engine-start record."""


def write_engine_start(state_dir: Path, record: EngineStartRecord) -> Path:
    """Install ``record`` as the engine's latest start; returns its path."""
    path = state_dir / ENGINE_START_FILENAME
    atomic_write_bytes(path, (record.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return path


def read_engine_start(state_dir: Path) -> EngineStartRecord:
    """The engine's latest start, as it recorded it."""
    path = state_dir / ENGINE_START_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise EngineStartRecordUnavailable(
            f"no {ENGINE_START_FILENAME} in {state_dir}: the engine has not started"
            " on a version that records its start; restart it"
        ) from error
    try:
        return EngineStartRecord.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise EngineStartRecordUnavailable(f"{path} is not an engine-start record: {error}") from error


__all__ = [
    "ENGINE_START_FILENAME",
    "EngineStartRecordUnavailable",
    "read_engine_start",
    "write_engine_start",
]
