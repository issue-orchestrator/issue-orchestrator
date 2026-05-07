from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import FakeIssueKey, GitHubIssueKey

SHA = "a" * 40


def test_attempt_key_rejects_empty_head_sha() -> None:
    with pytest.raises(ValueError, match="head_sha"):
        AttemptKey(GitHubIssueKey("owner/repo", "6130"), "")


def test_attempt_key_rejects_short_head_sha() -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        AttemptKey(GitHubIssueKey("owner/repo", "6130"), "abc123")


def test_attempt_round_trips_to_dict() -> None:
    key = AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA)
    attempt = Attempt(
        key=key,
        reroute_budget_used=2,
        validation_record_path=".issue-orchestrator/sessions/run/validation-record.json",
        review_exchange_summary_path=".issue-orchestrator/sessions/run/review-exchange/summary.json",
        review_exchange_job_id="review-exchange:6130:abc123",
    )

    restored = Attempt.from_dict(attempt.to_dict())

    assert restored.key.issue_stable_id == "6130"
    assert restored.key.issue_scope == "owner/repo"
    assert restored.key.head_sha == SHA
    assert isinstance(restored.key.issue_key, GitHubIssueKey)
    assert restored.reroute_budget_used == 2
    assert restored.validation_record_path == attempt.validation_record_path
    assert restored.review_exchange_summary_path == attempt.review_exchange_summary_path
    assert restored.review_exchange_job_id == attempt.review_exchange_job_id


def test_attempt_rejects_negative_reroute_budget() -> None:
    with pytest.raises(ValueError, match="reroute_budget_used"):
        Attempt(
            key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA),
            reroute_budget_used=-1,
        )


def test_attempt_is_immutable_after_validation() -> None:
    attempt = Attempt(key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA))

    with pytest.raises(FrozenInstanceError):
        setattr(attempt, "reroute_budget_used", -1)


def test_attempt_from_dict_rejects_unknown_schema_version() -> None:
    payload = Attempt(
        key=AttemptKey(GitHubIssueKey("owner/repo", "6130"), SHA)
    ).to_dict()
    payload["schema_version"] = 2

    with pytest.raises(ValueError, match="schema_version"):
        Attempt.from_dict(payload)


def test_attempt_to_dict_rejects_unsupported_issue_key_type() -> None:
    attempt = Attempt(key=AttemptKey(FakeIssueKey("6130"), SHA))

    with pytest.raises(ValueError, match="unsupported IssueKey type"):
        attempt.to_dict()
