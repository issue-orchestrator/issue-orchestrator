"""Strict wire codec for the shared Tech Lead pattern registry."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from ...domain.tech_lead_findings import (
    CaseFileClassification,
    PatternObservation,
    PendingCaseFile,
)
from ...ports.pattern_registry import (
    PatternRegistryEntry,
    PatternRegistryError,
    PendingPatternObservation,
)

PATTERN_REGISTRY_VERSION = 1
_TOP_LEVEL_FIELDS = frozenset(("version", "entries"))
_ENTRY_FIELDS = frozenset(
    (
        "signature",
        "reservation_id",
        "claimant_id",
        "expires_at",
        "pending",
        "issue_number",
        "observation_ids",
        "classification",
        "pending_observation",
        "publication_started_at",
    )
)
_PENDING_FIELDS = frozenset(
    (
        "signature",
        "title",
        "idempotency_marker",
        "body_observation_id",
        "fix_class",
        "area",
        "diagnosis",
    )
)
_CLASSIFICATION_FIELDS = frozenset(("fix_class", "area", "diagnosis"))
_PENDING_OBSERVATION_FIELDS = frozenset(("observation", "classification"))
_OBSERVATION_FIELDS = frozenset(("observation_id", "comment"))


def format_entries(entries: dict[str, PatternRegistryEntry]) -> str:
    payload = {
        "version": PATTERN_REGISTRY_VERSION,
        "entries": [_entry_to_dict(entries[key]) for key in sorted(entries)],
    }
    return "issue-orchestrator tech-lead pattern registry\n\n" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def parse_entries(message: str) -> dict[str, PatternRegistryEntry]:
    try:
        prefix, encoded = message.split("\n\n", 1)
        if prefix != "issue-orchestrator tech-lead pattern registry":
            raise ValueError("registry commit marker is missing")
        payload = json.loads(encoded, object_pairs_hook=_exact_object)
        if not isinstance(payload, dict):
            raise ValueError("registry payload must be an object")
        unexpected = set(payload).symmetric_difference(_TOP_LEVEL_FIELDS)
        if unexpected:
            raise ValueError(f"unexpected top-level fields: {sorted(unexpected)}")
        if not isinstance(payload.get("version"), int) or isinstance(
            payload.get("version"), bool
        ):
            raise ValueError("registry version must be an integer")
        if payload["version"] != PATTERN_REGISTRY_VERSION:
            raise ValueError(f"unsupported version {payload.get('version')!r}")
        if not isinstance(payload["entries"], list):
            raise ValueError("registry entries must be a list")
        entries = tuple(_entry_from_dict(item) for item in payload["entries"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PatternRegistryError(
            f"shared pattern registry is unreadable: {exc}"
        ) from exc
    mapped = {entry.signature: entry for entry in entries}
    if len(mapped) != len(entries):
        raise PatternRegistryError(
            "shared pattern registry contains duplicate signatures"
        )
    return mapped


def _entry_to_dict(entry: PatternRegistryEntry) -> dict[str, object]:
    return {
        "signature": entry.signature,
        "reservation_id": entry.reservation_id,
        "claimant_id": entry.claimant_id,
        "expires_at": entry.expires_at,
        "pending": asdict(entry.pending) if entry.pending is not None else None,
        "issue_number": entry.issue_number,
        "observation_ids": list(entry.observation_ids),
        "classification": asdict(entry.classification),
        "pending_observation": (
            asdict(entry.pending_observation)
            if entry.pending_observation is not None
            else None
        ),
        "publication_started_at": entry.publication_started_at,
    }


def _entry_from_dict(value: object) -> PatternRegistryEntry:
    if not isinstance(value, dict):
        raise ValueError("registry entry must be an object")
    _require_exact_fields(value, _ENTRY_FIELDS, "entry")
    expires_at = _required_text(value, "expires_at")
    datetime.fromisoformat(expires_at)
    publication_started_at = value["publication_started_at"]
    if publication_started_at is not None:
        if (
            not isinstance(publication_started_at, str)
            or not publication_started_at.strip()
        ):
            raise ValueError(
                "publication_started_at must be a non-empty string or null"
            )
        datetime.fromisoformat(publication_started_at)
    return PatternRegistryEntry(
        signature=_required_text(value, "signature"),
        reservation_id=_required_text(value, "reservation_id"),
        claimant_id=_required_text(value, "claimant_id"),
        expires_at=expires_at,
        pending=_decode_pending(value["pending"]),
        issue_number=_decode_issue_number(value["issue_number"]),
        observation_ids=_decode_observation_ids(value["observation_ids"]),
        classification=_decode_classification(value["classification"]),
        pending_observation=_decode_pending_observation(value["pending_observation"]),
        publication_started_at=publication_started_at,
    )


def _decode_pending(value: object) -> PendingCaseFile | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending must be an object or null")
    _require_exact_fields(value, _PENDING_FIELDS, "pending")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("every pending field must be a string")
    return PendingCaseFile(**value)


def _decode_classification(value: object) -> CaseFileClassification:
    if not isinstance(value, dict):
        raise ValueError("classification must be an object")
    _require_exact_fields(value, _CLASSIFICATION_FIELDS, "classification")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("every classification field must be a string")
    return CaseFileClassification(**value)


def _decode_pending_observation(value: object) -> PendingPatternObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending_observation must be an object or null")
    _require_exact_fields(value, _PENDING_OBSERVATION_FIELDS, "pending_observation")
    observation = value["observation"]
    if not isinstance(observation, dict):
        raise ValueError("pending observation payload must be an object")
    _require_exact_fields(observation, _OBSERVATION_FIELDS, "observation")
    if not all(isinstance(item, str) for item in observation.values()):
        raise ValueError("every observation field must be a string")
    return PendingPatternObservation(
        observation=PatternObservation(**observation),
        classification=_decode_classification(value["classification"]),
    )


def _decode_observation_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("observation_ids must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("observation_ids must be unique")
    return tuple(value)


def _decode_issue_number(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("issue_number must be an integer or null")
    return value


def _required_text(row: dict[str, object], name: str) -> str:
    value = row[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_exact_fields(
    row: dict[str, object], expected: frozenset[str], context: str
) -> None:
    unexpected = set(row).symmetric_difference(expected)
    if unexpected:
        raise ValueError(f"unexpected {context} fields: {sorted(unexpected)}")


def _exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for name, _value in pairs:
        if name in seen:
            raise ValueError(f"duplicate member name {name!r}")
        seen.add(name)
    return dict(pairs)
