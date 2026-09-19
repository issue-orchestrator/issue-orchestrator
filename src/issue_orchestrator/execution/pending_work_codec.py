"""Encoding for the durable pending-work claim artifact (#6999 F4).

The claim outlives the process that took it, so every field that exists nowhere
else has to survive the round trip: a failure investigation's typed
``DiscoveredFailure`` and its spent retry count, a validation retry's prompt,
error, attempt count and source task, a rework's PR number and cycle.

Encoding is explicit per kind rather than a generic ``asdict`` walk. A generic
walk silently degrades a type it does not understand — an enum to a bare value
it cannot rebuild, an ``IssueKey`` implementation to its private fields — and
the failure would only show up as work quietly reappearing wrong after a
restart. Every decoder here fails loudly on a payload it cannot rebuild.

``IssueKey`` round-trips as scope + stable id and returns as a
:class:`GitHubIssueKey`. That is not a downgrade: the protocol defines identity
as structural over exactly those two values, so the rebuilt key IS the same work
item by its own contract.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..domain.issue_key import GitHubIssueKey, IssueKey
from ..domain.models import (
    DiscoveredFailure,
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
)
from ..domain.pending_work import (
    PendingWorkClaim,
    PendingWorkKind,
    PendingWorkRequest,
)
from ..domain.session_key import TaskKind
from ..domain.session_run import SessionRunIdentity
from ..domain.tech_lead_scratch_identity import names_one_scratch_checkout
from ..domain.tech_lead_session import TechLeadSessionFlavor

CLAIM_ARTIFACT_NAME = "pending-work-claim.json"
# Bumped only when an encoding change cannot be read by the previous decoder.
# A payload from a different version is refused rather than guessed at.
CLAIM_SCHEMA_VERSION = 2
#: Versions this build can still READ. A schema-v1 INVESTIGATION retry recorded
#: no authority provenance at all, so it decodes as explicitly unlaunchable
#: until durable checkout recovery joins it to its original allocation. Letting
#: it decode as ordinary launchable work meant a pruned-artifact or live-terminal
#: case could relaunch with no grant and fail as `missing_authority` -- the
#: defect this PR exists to fix, surviving the upgrade (round 16 finding 1).
READABLE_CLAIM_SCHEMA_VERSIONS = frozenset({1, CLAIM_SCHEMA_VERSION})


class PendingWorkClaimDecodeError(ValueError):
    """A stored claim could not be rebuilt into its original typed request."""


def encode_claim(claim: PendingWorkClaim) -> dict[str, Any]:
    """Encode a claim for durable storage."""
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "kind": claim.kind.value,
        "request": _ENCODERS[claim.kind](claim.request),
    }


def decode_claim(payload: object) -> PendingWorkClaim:
    """Rebuild a claim, raising when the payload cannot produce the original."""
    if not isinstance(payload, dict):
        raise PendingWorkClaimDecodeError(
            f"claim payload must be an object, got {type(payload).__name__}"
        )
    version = payload.get("schema_version")
    if version not in READABLE_CLAIM_SCHEMA_VERSIONS:
        raise PendingWorkClaimDecodeError(
            f"unsupported claim schema version {version!r}; "
            f"this build writes {CLAIM_SCHEMA_VERSION}"
        )
    try:
        kind = PendingWorkKind(payload["kind"])
    except (KeyError, ValueError) as exc:
        raise PendingWorkClaimDecodeError(
            f"claim payload has no known pending work kind: {payload.get('kind')!r}"
        ) from exc
    request = payload.get("request")
    if not isinstance(request, dict):
        raise PendingWorkClaimDecodeError(
            f"{kind.value} claim payload has no request object"
        )
    try:
        decoded = _DECODERS[kind](request)
    except PendingWorkClaimDecodeError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PendingWorkClaimDecodeError(
            f"{kind.value} claim payload could not be rebuilt: {exc}"
        ) from exc
    if (
        version == 1
        and isinstance(decoded, PendingValidationRetry)
        and names_one_scratch_checkout(
            Path(decoded.worktree_path).name, decoded.branch_name or ""
        )
    ):
        decoded = replace(
            decoded,
            recovery_error=(
                "Schema-v1 investigation retry has no recorded launch-authority "
                "provenance; durable checkout recovery must join it to its "
                "original allocation before relaunch"
            ),
        )
    return PendingWorkClaim(kind, decoded)


def _encode_issue_key(key: IssueKey) -> dict[str, str]:
    return {"scope": key.scope(), "stable_id": str(key.stable_id())}


def _decode_issue_key(payload: object) -> IssueKey:
    if not isinstance(payload, dict):
        raise PendingWorkClaimDecodeError("issue key payload must be an object")
    scope = str(payload["scope"])
    # Same contract as the attempt sidecar (`domain/attempt.py`) and the run
    # ledger: a stored key with no repository scope is refused on the way OUT,
    # not carried into the domain. `GitHubIssueKey.scope()` raises, so an
    # unscoped claim decoded here would instead fail wherever it was eventually
    # read - `_encode_issue_key` on the next save, or
    # `pending_work_successors` - far from the bad row (#7255).
    if not scope.strip():
        raise PendingWorkClaimDecodeError(
            f"stored claim for issue {payload.get('stable_id')!r} has no "
            "repository scope"
        )
    return GitHubIssueKey(repo=scope, external_id=str(payload["stable_id"]))


def _encode_review(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingReview)
    return {
        "issue_key": _encode_issue_key(request.issue_key),
        "pr_number": request.pr_number,
        "pr_url": request.pr_url,
        "branch_name": request.branch_name,
        "issue_number": request.issue_number,
        "agent_label": request.agent_label,
        "issue_labels": list(request.issue_labels),
    }


def _decode_review(payload: dict[str, Any]) -> PendingReview:
    return PendingReview(
        issue_key=_decode_issue_key(payload["issue_key"]),
        pr_number=int(payload["pr_number"]),
        pr_url=str(payload["pr_url"]),
        branch_name=str(payload["branch_name"]),
        _issue_number=int(payload["issue_number"]),
        agent_label=payload["agent_label"],
        issue_labels=tuple(payload["issue_labels"]),
    )


def _encode_retrospective_review(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingRetrospectiveReview)
    return {
        "issue_key": _encode_issue_key(request.issue_key),
        "issue_number": request.issue_number,
        "issue_title": request.issue_title,
        "agent_label": request.agent_label,
        "trigger_label": request.trigger_label,
        "prior_pr_number": request.prior_pr_number,
        "prior_pr_url": request.prior_pr_url,
        "issue_labels": list(request.issue_labels),
    }


def _decode_retrospective_review(
    payload: dict[str, Any],
) -> PendingRetrospectiveReview:
    return PendingRetrospectiveReview(
        issue_key=_decode_issue_key(payload["issue_key"]),
        issue_number=int(payload["issue_number"]),
        issue_title=str(payload["issue_title"]),
        agent_label=str(payload["agent_label"]),
        trigger_label=str(payload["trigger_label"]),
        prior_pr_number=payload["prior_pr_number"],
        prior_pr_url=payload["prior_pr_url"],
        issue_labels=tuple(payload["issue_labels"]),
    )


def _encode_rework(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingRework)
    return {
        "issue_key": _encode_issue_key(request.issue_key),
        "agent_type": request.agent_type,
        "rework_cycle": request.rework_cycle,
        "issue_number": request.issue_number,
        "pr_number": request.pr_number,
        "source": request.source,
        "feedback": request.feedback,
        "scoped_request_keys": list(request.scoped_request_keys),
    }


def _decode_rework(payload: dict[str, Any]) -> PendingRework:
    return PendingRework(
        issue_key=_decode_issue_key(payload["issue_key"]),
        agent_type=str(payload["agent_type"]),
        rework_cycle=int(payload["rework_cycle"]),
        issue_number=payload["issue_number"],
        pr_number=payload["pr_number"],
        source=str(payload["source"]),
        feedback=payload["feedback"],
        scoped_request_keys=tuple(payload.get("scoped_request_keys", ())),
    )


def _encode_validation_retry(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingValidationRetry)
    return {
        "issue_number": request.issue_number,
        "issue_title": request.issue_title,
        "agent_label": request.agent_label,
        "worktree_path": request.worktree_path,
        "branch_name": request.branch_name,
        "original_prompt": request.original_prompt,
        "validation_error": request.validation_error,
        "validation_error_file": request.validation_error_file,
        "retry_count": request.retry_count,
        "source_task": request.source_task.value,
        "validation_cmd": request.validation_cmd,
        "authority_run": (
            {
                "session_name": request.authority_run.session_name,
                "run_id": request.authority_run.run_id,
                "started_at": request.authority_run.started_at,
            }
            if request.authority_run is not None
            else None
        ),
        "recovery_error": request.recovery_error,
    }


def _decode_validation_retry(payload: dict[str, Any]) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=int(payload["issue_number"]),
        issue_title=str(payload["issue_title"]),
        agent_label=str(payload["agent_label"]),
        worktree_path=str(payload["worktree_path"]),
        branch_name=str(payload["branch_name"]),
        original_prompt=payload["original_prompt"],
        validation_error=str(payload["validation_error"]),
        validation_error_file=payload["validation_error_file"],
        retry_count=int(payload["retry_count"]),
        source_task=TaskKind(payload["source_task"]),
        validation_cmd=payload["validation_cmd"],
        authority_run=_decode_run_identity(payload.get("authority_run")),
        recovery_error=payload.get("recovery_error"),
    )


def _decode_run_identity(payload: object) -> SessionRunIdentity | None:
    """The run a retry inherits authority from, when it recorded one.

    Absent for a retry queued before #7273, and for every non-tech-lead retry,
    which have no authority row to inherit.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"authority_run is not an object: {payload!r}")
    return SessionRunIdentity(
        session_name=str(payload["session_name"]),
        run_id=str(payload["run_id"]),
        started_at=str(payload["started_at"]),
    )


def _encode_tech_lead(request: PendingWorkRequest) -> dict[str, Any]:
    assert isinstance(request, PendingTechLeadReview)
    return {
        "issue_number": request.issue_number,
        "title": request.title,
        "flavor": request.flavor.value,
        "failure": request.failure.to_dict() if request.failure else None,
        "problem_cohort": [
            failure.to_dict() for failure in request.problem_cohort
        ],
        "retryable_launch_failures": request.retryable_launch_failures,
    }


def _decode_tech_lead(payload: dict[str, Any]) -> PendingTechLeadReview:
    failure_payload = payload["failure"]
    item = PendingTechLeadReview(
        issue_number=int(payload["issue_number"]),
        title=str(payload["title"]),
        flavor=TechLeadSessionFlavor(payload["flavor"]),
        failure=(
            DiscoveredFailure.from_dict(failure_payload)
            if failure_payload is not None
            else None
        ),
        problem_cohort=tuple(
            DiscoveredFailure.from_dict(entry)
            for entry in payload["problem_cohort"]
        ),
    )
    # Not a constructor argument: the retry budget is owner-tracked state, and
    # a restart must not silently refund what a previous process already spent.
    item.retryable_launch_failures = int(payload["retryable_launch_failures"])
    return item


_ENCODERS: dict[PendingWorkKind, Callable[[PendingWorkRequest], dict[str, Any]]] = {
    PendingWorkKind.REVIEW: _encode_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _encode_retrospective_review,
    PendingWorkKind.REWORK: _encode_rework,
    PendingWorkKind.VALIDATION_RETRY: _encode_validation_retry,
    PendingWorkKind.TECH_LEAD: _encode_tech_lead,
}

_DECODERS: dict[PendingWorkKind, Callable[[dict[str, Any]], PendingWorkRequest]] = {
    PendingWorkKind.REVIEW: _decode_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _decode_retrospective_review,
    PendingWorkKind.REWORK: _decode_rework,
    PendingWorkKind.VALIDATION_RETRY: _decode_validation_retry,
    PendingWorkKind.TECH_LEAD: _decode_tech_lead,
}

# Every kind must be encodable AND decodable. A kind added to the enum without
# both halves would otherwise only fail at the moment a real session held it.
assert set(_ENCODERS) == set(PendingWorkKind)
assert set(_DECODERS) == set(PendingWorkKind)


__all__ = [
    "CLAIM_ARTIFACT_NAME",
    "CLAIM_SCHEMA_VERSION",
    "PendingWorkClaimDecodeError",
    "decode_claim",
    "encode_claim",
]
