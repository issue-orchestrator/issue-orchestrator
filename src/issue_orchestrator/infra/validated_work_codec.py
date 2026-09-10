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
    RemoteBaselineStatus,
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


def _decode_identity(identity: dict[str, Any]) -> ValidatedWorkIdentity:
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
    return ValidatedWorkIdentity(**identity)


def _decode_observations(
    observations: dict[str, Any], *, legacy_shape_only: bool
) -> ValidatedWorkObservations:
    legacy = "remote_baseline_status" not in observations
    if legacy_shape_only:
        if not legacy:
            raise ValueError("legacy observations unexpectedly carry remote authority")
        # This value exists only while validating the historical byte shape.
        # Before the provenance bit existed, the capture gate treated these
        # values as authoritative. The returned runtime evidence is decoded
        # separately below and always discards that unproved authority.
        observations["remote_baseline_status"] = RemoteBaselineStatus.OBSERVED
    else:
        observations["remote_baseline_status"] = RemoteBaselineStatus(
            observations.get("remote_baseline_status", RemoteBaselineStatus.UNOBSERVED)
        )
        if legacy:
            # Older rows never proved whether null meant branch absence or a
            # read that never happened. Discard their apparent authority
            # together: refresh must rebuild branch and PR facts as one read.
            observations["expected_remote_head_sha"] = None
            observations["pr_number"] = None
    if not isinstance(observations["observed_blocking_labels"], list):
        raise ValueError("observed_blocking_labels must be an array")
    observations["observed_blocking_labels"] = tuple(
        observations["observed_blocking_labels"]
    )
    observations["admitted_from_paths"] = {
        ArtifactSlot(k): v for k, v in observations["admitted_from_paths"].items()
    }
    return ValidatedWorkObservations(**observations)


def _decode_evidence(
    identity_json: str, observations_json: str, *, legacy_shape_only: bool
) -> ValidatedWorkEvidence:
    identity = _decode_identity(load_document(identity_json))
    observations = _decode_observations(
        load_document(observations_json), legacy_shape_only=legacy_shape_only
    )
    return ValidatedWorkEvidence(identity, observations)


def decode_evidence(
    identity_json: str, observations_json: str
) -> ValidatedWorkEvidence:
    return _decode_evidence(identity_json, observations_json, legacy_shape_only=False)


def decode_legacy_evidence_shape(
    identity_json: str, observations_json: str
) -> ValidatedWorkEvidence:
    """Decode historical bytes without granting their remote facts authority."""
    return _decode_evidence(identity_json, observations_json, legacy_shape_only=True)
