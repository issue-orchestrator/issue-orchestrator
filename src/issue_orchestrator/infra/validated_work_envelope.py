"""Versioned checksummed envelope, decoded through the shared evidence codec."""

import hashlib

from ..domain.validated_work import (
    ValidatedWorkFailure,
    ValidatedWorkState,
    canonical_json,
)
from ..domain.validated_work_escrow import validate_capture_locations
from ..domain.validated_work_store import EvidenceAdmission
from .validated_work_codec import decode_evidence, load_document


def encode_envelope(admission: EvidenceAdmission) -> bytes:
    validate_capture_locations(admission)
    admission.require_capture_gate()
    payload = {
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
    checksum = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return canonical_json({**payload, "checksum": checksum}).encode()


def decode_envelope(data: bytes) -> EvidenceAdmission:
    payload = load_document(data)
    checksum = payload.pop("checksum")
    if checksum != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
        raise ValueError("capture envelope checksum mismatch")
    if type(payload["envelope_version"]) is not int or payload["envelope_version"] != 1:
        raise ValueError("unsupported capture envelope version")
    evidence = decode_evidence(
        canonical_json(payload["identity"]), canonical_json(payload["observations"])
    )
    if (payload["record_id"], payload["evidence_id"]) != (
        evidence.record_id,
        evidence.evidence_id,
    ):
        raise ValueError("capture identity does not match recomputed IDs")
    admission = EvidenceAdmission(
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
    # Also rejects unknown fields, omitted nulls and normalization-changing inputs.
    if load_document(encode_envelope(admission)) != {**payload, "checksum": checksum}:
        raise ValueError("noncanonical capture envelope")
    return admission
