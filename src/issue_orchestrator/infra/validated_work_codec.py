"""Shared, strict typed evidence decoding for SQLite and capture envelopes."""

import json
from typing import Any

from ..domain.models import RequestedAction
from ..domain.review_exchange_summary import ReviewExchangeTerminalState
from ..domain.session_run import SessionRunIdentity
from ..domain.validated_work import (
    AdmittedArtifact,
    ArtifactSlot,
    ReviewDisposition,
    ValidatedWorkEvidence,
    ValidatedWorkIdentity,
    ValidatedWorkKey,
    ValidatedWorkObservations,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_document(value: str | bytes) -> dict[str, Any]:
    result = json.loads(value, object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def decode_evidence(
    identity_json: str, observations_json: str
) -> ValidatedWorkEvidence:
    identity = load_document(identity_json)
    for name in (
        "completion_artifact",
        "validation_artifact",
        "exchange_summary_artifact",
    ):
        raw = identity[name]
        if raw is not None:
            identity[name] = AdmittedArtifact(
                **{**raw, "slot": ArtifactSlot(raw["slot"])}
            )
    identity["key"] = ValidatedWorkKey(**identity["key"])
    identity["run_identity"] = SessionRunIdentity(**identity["run_identity"])
    if not isinstance(identity["requested_actions"], list):
        raise ValueError("requested_actions must be an array")
    identity["requested_actions"] = tuple(
        RequestedAction(v) for v in identity["requested_actions"]
    )
    identity["review_disposition"] = ReviewDisposition(identity["review_disposition"])
    if identity["exchange_terminal"] is not None:
        identity["exchange_terminal"] = ReviewExchangeTerminalState(
            **identity["exchange_terminal"]
        )
    observations = load_document(observations_json)
    if not isinstance(observations["observed_blocking_labels"], list):
        raise ValueError("observed_blocking_labels must be an array")
    observations["observed_blocking_labels"] = tuple(
        observations["observed_blocking_labels"]
    )
    observations["admitted_from_paths"] = {
        ArtifactSlot(k): v for k, v in observations["admitted_from_paths"].items()
    }
    return ValidatedWorkEvidence(
        ValidatedWorkIdentity(**identity), ValidatedWorkObservations(**observations)
    )
