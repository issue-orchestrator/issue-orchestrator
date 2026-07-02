"""Behavior of the submission-scoped republish job identity."""

from __future__ import annotations

from issue_orchestrator.control.republish_job_id import RepublishJobId


def test_encode_parse_round_trip() -> None:
    original = RepublishJobId(issue_number=6673, token=7)
    restored = RepublishJobId.parse(original.encode())
    assert restored == original


def test_distinct_tokens_produce_distinct_job_ids() -> None:
    # Two retries for the same issue must not collide on the runner.
    first = RepublishJobId(issue_number=42, token=0).encode()
    second = RepublishJobId(issue_number=42, token=1).encode()
    assert first != second


def test_parse_rejects_non_republish_id() -> None:
    assert RepublishJobId.parse("review-exchange:42:coding-1") is None


def test_parse_rejects_malformed_suffix() -> None:
    assert RepublishJobId.parse("republish:42:not-an-int") is None
    assert RepublishJobId.parse("republish:onlyissue") is None
