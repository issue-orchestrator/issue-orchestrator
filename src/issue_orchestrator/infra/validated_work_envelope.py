"""Versioned checksummed envelope, decoded through the shared evidence codec."""

import hashlib
from typing import Any

from ..domain.validated_work import (
    ValidatedWorkFailure,
    ValidatedWorkEvidence,
    ValidatedWorkState,
    canonical_json,
)
from ..domain.validated_work_escrow import validate_capture_locations
from ..domain.validated_work_store import EvidenceAdmission
from .validated_work_codec import (
    decode_evidence,
    decode_legacy_evidence_shape,
    load_document,
)
from .validated_work_legacy import normalize_legacy_remote_authority


def _payload(admission: EvidenceAdmission) -> dict[str, Any]:
    return {
        "envelope_version": 1,
        "record_id": admission.evidence.record_id,
        "evidence_id": admission.evidence.evidence_id,
        "identity": admission.evidence.identity,
        "observations": admission.evidence.observations,
        "initial_state": admission.initial_state,
        "initial_failure": admission.initial_failure,
        "initial_reason": admission.initial_reason,
        "escrow_dir": admission.escrow_dir,
        "pinned_ref": admission.pinned_ref,
        "observed_ref": admission.observed_ref,
        "admitted_at": admission.admitted_at,
    }


def _document(payload: dict[str, Any]) -> dict[str, Any]:
    checksum = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return {**payload, "checksum": checksum}


def encode_envelope(admission: EvidenceAdmission) -> bytes:
    validate_capture_locations(admission)
    admission.require_capture_gate()
    return canonical_json(_document(_payload(admission))).encode()


def _admission(
    payload: dict[str, Any], evidence: ValidatedWorkEvidence
) -> EvidenceAdmission:
    return EvidenceAdmission(
        evidence,
        ValidatedWorkState(payload["initial_state"]),
        ValidatedWorkFailure(payload["initial_failure"])
        if payload["initial_failure"] is not None
        else None,
        payload["initial_reason"],
        payload["escrow_dir"],
        payload["pinned_ref"],
        payload["observed_ref"],
        payload["admitted_at"],
    )


def _require_identity(payload: dict[str, Any], evidence: ValidatedWorkEvidence) -> None:
    if (payload["record_id"], payload["evidence_id"]) != (
        evidence.record_id,
        evidence.evidence_id,
    ):
        raise ValueError("capture identity does not match recomputed IDs")


def _decode_legacy_envelope(
    payload: dict[str, Any], checksum: str
) -> EvidenceAdmission:
    identity_json = canonical_json(payload["identity"])
    observations_json = canonical_json(payload["observations"])

    # First validate the exact historical byte shape and its former capture
    # gate. The synthetic observed bit is discarded immediately afterward and
    # can never authorize runtime publication.
    historical_evidence = decode_legacy_evidence_shape(identity_json, observations_json)
    _require_identity(payload, historical_evidence)
    historical = _admission(payload, historical_evidence)
    validate_capture_locations(historical)
    historical.require_capture_gate()
    historical_payload = _payload(historical)
    historical_payload["observations"] = load_document(
        canonical_json(historical_payload["observations"])
    )
    historical_payload["observations"].pop("remote_baseline_status")
    if load_document(canonical_json(_document(historical_payload))) != {
        **payload,
        "checksum": checksum,
    }:
        raise ValueError("noncanonical capture envelope")

    evidence = decode_evidence(identity_json, observations_json)
    state, failure, reason = normalize_legacy_remote_authority(
        ValidatedWorkState(payload["initial_state"]),
        ValidatedWorkFailure(payload["initial_failure"])
        if payload["initial_failure"] is not None
        else None,
        payload["initial_reason"],
    )
    admission = EvidenceAdmission(
        evidence,
        state,
        failure,
        reason,
        payload["escrow_dir"],
        payload["pinned_ref"],
        payload["observed_ref"],
        payload["admitted_at"],
    )
    validate_capture_locations(admission)
    admission.require_capture_gate()
    return admission


def decode_envelope(data: bytes) -> EvidenceAdmission:
    payload = load_document(data)
    checksum = payload.pop("checksum")
    if checksum != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
        raise ValueError("capture envelope checksum mismatch")
    if type(payload["envelope_version"]) is not int or payload["envelope_version"] != 1:
        raise ValueError("unsupported capture envelope version")
    if (
        isinstance(payload["observations"], dict)
        and "remote_baseline_status" not in payload["observations"]
    ):
        return _decode_legacy_envelope(payload, checksum)
    evidence = decode_evidence(
        canonical_json(payload["identity"]), canonical_json(payload["observations"])
    )
    _require_identity(payload, evidence)
    admission = _admission(payload, evidence)
    # Also rejects unknown fields, omitted nulls and normalization-changing inputs.
    if load_document(encode_envelope(admission)) != {**payload, "checksum": checksum}:
        raise ValueError("noncanonical capture envelope")
    return admission
