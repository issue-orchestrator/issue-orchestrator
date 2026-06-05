"""Typed review-exchange summary artifact contract."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping


class ReviewExchangeStatus(StrEnum):
    """Terminal status values persisted in ``review-exchange/summary.json``."""

    OK = "ok"
    STOPPED = "stopped"
    ERROR = "error"


class ReviewExchangeReason(StrEnum):
    """Terminal reason values persisted in ``review-exchange/summary.json``."""

    REVIEWER_OK = "reviewer_ok"
    REVIEWER_REPORTS_NO_PROGRESS = "reviewer_reports_no_progress"
    MAX_ROUNDS_EXCEEDED = "max_rounds_exceeded"
    REVIEWER_NO_COMPLETION = "reviewer_no_completion"
    CODER_NO_COMPLETION = "coder_no_completion"
    CODER_PROTOCOL_ERROR = "coder_protocol_error"
    REVIEWER_DECISION_INVALID = "reviewer_decision_invalid"


VALID_REVIEW_EXCHANGE_TERMINALS: frozenset[
    tuple[ReviewExchangeStatus, ReviewExchangeReason]
] = frozenset(
    {
        (ReviewExchangeStatus.OK, ReviewExchangeReason.REVIEWER_OK),
        (
            ReviewExchangeStatus.STOPPED,
            ReviewExchangeReason.REVIEWER_REPORTS_NO_PROGRESS,
        ),
        (ReviewExchangeStatus.STOPPED, ReviewExchangeReason.MAX_ROUNDS_EXCEEDED),
        (ReviewExchangeStatus.ERROR, ReviewExchangeReason.REVIEWER_NO_COMPLETION),
        (ReviewExchangeStatus.ERROR, ReviewExchangeReason.CODER_NO_COMPLETION),
        (ReviewExchangeStatus.ERROR, ReviewExchangeReason.CODER_PROTOCOL_ERROR),
        (ReviewExchangeStatus.ERROR, ReviewExchangeReason.REVIEWER_DECISION_INVALID),
    }
)


@dataclass(frozen=True, slots=True)
class ReviewExchangeTerminalState:
    """Enum-backed terminal status/reason pair."""

    status: ReviewExchangeStatus
    reason: ReviewExchangeReason

    def __post_init__(self) -> None:
        status = ReviewExchangeStatus(self.status)
        reason = ReviewExchangeReason(self.reason)
        if (status, reason) not in VALID_REVIEW_EXCHANGE_TERMINALS:
            raise ValueError(
                "invalid review-exchange terminal state: "
                f"status={status.value!r} reason={reason.value!r}"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)

    @classmethod
    def from_values(
        cls,
        status: object,
        reason: object,
    ) -> "ReviewExchangeTerminalState":
        try:
            return cls(
                status=ReviewExchangeStatus(status),
                reason=ReviewExchangeReason(reason),
            )
        except ValueError as exc:
            raise ValueError(
                "invalid review-exchange terminal state: "
                f"status={status!r} reason={reason!r}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ReviewExchangeSummaryArtifactRef:
    """Typed artifact reference embedded in a review-exchange summary."""

    artifact_type: str
    label: str
    value: str
    render_mode: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.artifact_type, "artifact_type")
        _require_non_empty_str(self.label, "label")
        _require_non_empty_str(self.value, "value")
        if self.render_mode is not None:
            _require_non_empty_str(self.render_mode, "render_mode")

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "ReviewExchangeSummaryArtifactRef":
        artifact_type = _required_str(payload, "type")
        label = _required_str(payload, "label")
        value = _required_str(payload, "value")
        render_mode = _optional_str(payload, "render_mode")
        return cls(
            artifact_type=artifact_type,
            label=label,
            value=value,
            render_mode=render_mode,
        )

    def to_payload(self) -> dict[str, str]:
        payload = {
            "type": self.artifact_type,
            "label": self.label,
            "value": self.value,
        }
        if self.render_mode is not None:
            payload["render_mode"] = self.render_mode
        return payload


@dataclass(frozen=True, slots=True)
class ReviewExchangeSummaryV1:
    """Frozen v1 summary payload for ``review-exchange/summary.json``."""

    completed_rounds: int
    terminal: ReviewExchangeTerminalState
    response_text: str | None
    timestamp: str
    head_sha: str | None = None
    validation_passed: bool | None = None
    artifacts: tuple[ReviewExchangeSummaryArtifactRef, ...] = field(
        default_factory=tuple,
    )
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.completed_rounds) is not int:
            raise TypeError("completed_rounds must be an int")
        if self.completed_rounds < 0:
            raise ValueError("completed_rounds must be >= 0")
        _require_non_empty_str(self.timestamp, "timestamp")
        if self.response_text is not None:
            _require_str(self.response_text, "response_text")
        if self.head_sha is not None:
            _require_non_empty_str(self.head_sha, "head_sha")
        if (
            self.validation_passed is not None
            and type(self.validation_passed) is not bool
        ):
            raise TypeError("validation_passed must be bool or None")
        if self.detail is not None:
            _require_non_empty_str(self.detail, "detail")
        object.__setattr__(self, "terminal", _coerce_terminal(self.terminal))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        for artifact in self.artifacts:
            if type(artifact) is not ReviewExchangeSummaryArtifactRef:
                raise TypeError(
                    "artifacts must contain ReviewExchangeSummaryArtifactRef"
                )

    @property
    def status(self) -> ReviewExchangeStatus:
        return self.terminal.status

    @property
    def reason(self) -> ReviewExchangeReason:
        return self.terminal.reason

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "ReviewExchangeSummaryV1":
        completed_rounds = payload.get("completed_rounds")
        if not isinstance(completed_rounds, int) or isinstance(
            completed_rounds,
            bool,
        ):
            raise ValueError("review-exchange summary requires int completed_rounds")
        terminal = ReviewExchangeTerminalState.from_values(
            payload.get("status"),
            payload.get("reason"),
        )
        response_text = payload.get("response_text")
        if response_text is not None and not isinstance(response_text, str):
            raise ValueError("review-exchange summary response_text must be str/null")
        timestamp = _required_str(payload, "timestamp")
        artifacts = _artifact_refs_from_payload(payload.get("artifacts"))
        return cls(
            completed_rounds=completed_rounds,
            terminal=terminal,
            response_text=response_text,
            timestamp=timestamp,
            head_sha=_optional_str(payload, "head_sha"),
            validation_passed=_optional_bool(payload, "validation_passed"),
            artifacts=artifacts,
            detail=_optional_str(payload, "detail"),
        )

    def with_head_sha_if_missing(
        self, head_sha: str | None
    ) -> "ReviewExchangeSummaryV1":
        if self.head_sha is not None or head_sha is None or not head_sha.strip():
            return self
        return replace(self, head_sha=head_sha.strip())

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "completed_rounds": self.completed_rounds,
            "status": self.status.value,
            "response_text": self.response_text,
            "reason": self.reason.value,
            "timestamp": self.timestamp,
        }
        if self.head_sha is not None:
            payload["head_sha"] = self.head_sha
        if self.validation_passed is not None:
            payload["validation_passed"] = self.validation_passed
        if self.artifacts:
            payload["artifacts"] = [
                artifact.to_payload() for artifact in self.artifacts
            ]
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def _coerce_terminal(value: object) -> ReviewExchangeTerminalState:
    if isinstance(value, ReviewExchangeTerminalState):
        return value
    raise TypeError("terminal must be ReviewExchangeTerminalState")


def _artifact_refs_from_payload(
    raw: object,
) -> tuple[ReviewExchangeSummaryArtifactRef, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("review-exchange summary artifacts must be a list")
    artifacts: list[ReviewExchangeSummaryArtifactRef] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("review-exchange summary artifacts must be object entries")
        artifacts.append(ReviewExchangeSummaryArtifactRef.from_payload(item))
    return tuple(artifacts)


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"review-exchange summary requires non-empty {key}")
    return value


def _optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"review-exchange summary {key} must be non-empty str")
    return value


def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"review-exchange summary {key} must be bool")
    return value


def _require_str(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")


def _require_non_empty_str(value: object, field_name: str) -> None:
    _require_str(value, field_name)
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
