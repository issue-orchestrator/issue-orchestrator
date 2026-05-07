"""Attempt-scoped state for one issue at one commit.

An ``Attempt`` is the cache boundary named in issue #6130: all facts here are
about a specific issue at a specific HEAD SHA. Per-run artifacts remain on the
session run manifest; cross-run cache facts belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .issue_key import GitHubIssueKey, IssueKey, StableIssueId

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredIssueKey:
    """IssueKey implementation reconstructed from an attempt sidecar."""

    stable: str
    key_scope: str

    def stable_id(self) -> StableIssueId:
        return StableIssueId(self.stable)

    def scope(self) -> str:
        return self.key_scope

    def __str__(self) -> str:
        return f"{self.key_scope}:{self.stable}"


@dataclass(frozen=True)
class AttemptKey:
    """Stable identity for an issue attempt at a specific commit."""

    issue_key: IssueKey
    head_sha: str

    def __post_init__(self) -> None:
        normalized = self.head_sha.strip().lower()
        if not _FULL_SHA_RE.fullmatch(normalized):
            raise ValueError("AttemptKey.head_sha must be a full 40-character hex SHA")
        object.__setattr__(self, "head_sha", normalized)

    @property
    def issue_stable_id(self) -> str:
        return str(self.issue_key.stable_id())

    @property
    def issue_scope(self) -> str:
        return self.issue_key.scope()


@dataclass(frozen=True)
class Attempt:
    """Authoritative per-attempt state for an issue at a specific commit."""

    key: AttemptKey
    reroute_budget_used: int = 0
    validation_record_path: str | None = None
    review_exchange_summary_path: str | None = None
    review_exchange_job_id: str | None = None

    def __post_init__(self) -> None:
        if self.reroute_budget_used < 0:
            raise ValueError("Attempt.reroute_budget_used must be >= 0")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "issue_key_type": _issue_key_type(self.key.issue_key),
            "issue_key": self.key.issue_stable_id,
            "issue_scope": self.key.issue_scope,
            "head_sha": self.key.head_sha,
            "reroute_budget_used": self.reroute_budget_used,
            "validation_record_path": self.validation_record_path,
            "review_exchange_summary_path": self.review_exchange_summary_path,
            "review_exchange_job_id": self.review_exchange_job_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Attempt":
        _validate_schema_version(data.get("schema_version"))
        issue_key_type_raw = data.get("issue_key_type")
        issue_key_raw = data.get("issue_key")
        issue_scope_raw = data.get("issue_scope")
        head_sha = data.get("head_sha")
        if not isinstance(issue_key_type_raw, str) or not issue_key_type_raw.strip():
            raise ValueError("Attempt sidecar missing issue_key_type")
        if not isinstance(issue_key_raw, str) or not issue_key_raw.strip():
            raise ValueError("Attempt sidecar missing issue_key")
        if not isinstance(issue_scope_raw, str) or not issue_scope_raw.strip():
            raise ValueError("Attempt sidecar missing issue_scope")
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise ValueError("Attempt sidecar missing head_sha")
        return cls(
            key=AttemptKey(
                _issue_key_from_dict(
                    issue_key_type=issue_key_type_raw,
                    stable_id=issue_key_raw,
                    scope=issue_scope_raw,
                ),
                head_sha,
            ),
            reroute_budget_used=_int_field(
                data.get("reroute_budget_used"), "reroute_budget_used"
            ),
            validation_record_path=_optional_str(data.get("validation_record_path")),
            review_exchange_summary_path=_optional_str(
                data.get("review_exchange_summary_path")
            ),
            review_exchange_job_id=_optional_str(data.get("review_exchange_job_id")),
        )


def _issue_key_type(issue_key: IssueKey) -> str:
    if isinstance(issue_key, GitHubIssueKey):
        return "github"
    if isinstance(issue_key, StoredIssueKey):
        return "stored"
    raise ValueError(
        f"Attempt cannot persist unsupported IssueKey type: {type(issue_key).__name__}"
    )


def _issue_key_from_dict(
    *,
    issue_key_type: str,
    stable_id: str,
    scope: str,
) -> IssueKey:
    match issue_key_type.strip().lower():
        case "github":
            return GitHubIssueKey(repo=scope, external_id=stable_id)
        case "stored":
            return StoredIssueKey(stable_id, scope)
        case other:
            raise ValueError(f"unknown Attempt issue_key_type: {other}")


def _validate_schema_version(value: object) -> None:
    if isinstance(value, bool) or value != _SCHEMA_VERSION:
        raise ValueError(f"Attempt sidecar schema_version must be {_SCHEMA_VERSION}")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected string or null, got {type(value).__name__}")
    stripped = value.strip()
    return stripped or None


def _int_field(value: object, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value
