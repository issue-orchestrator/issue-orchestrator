"""Bounds and shape validation for CompletionRecord.from_dict.

The orchestrator treats completion records as untrusted input. These tests
cover the static bounds and shape checks added for security issue #5987
(findings F1, F2, F7): validation_record_path traversal, unbounded string
fields, and unbounded list fields.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    ProposedFollowUpIssue,
)


def _minimal_payload(**overrides: Any) -> dict[str, Any]:
    """A valid baseline completion record payload."""
    payload: dict[str, Any] = {
        "session_id": "session-42",
        "timestamp": "2026-04-22T00:00:00Z",
        "outcome": CompletionOutcome.COMPLETED.value,
        "summary": "ok",
    }
    payload.update(overrides)
    return payload


class TestValidationRecordPath:
    """F1 — reject path traversal and absolute paths."""

    def test_happy_path_relative(self):
        rec = CompletionRecord.from_dict(
            _minimal_payload(
                validation_record_path=".issue-orchestrator/validation-record.json",
            )
        )
        assert rec.validation_record_path == (
            ".issue-orchestrator/validation-record.json"
        )

    def test_rejects_absolute_posix(self):
        with pytest.raises(ValueError, match="relative"):
            CompletionRecord.from_dict(
                _minimal_payload(validation_record_path="/etc/passwd")
            )

    def test_rejects_absolute_windows_like(self):
        with pytest.raises(ValueError, match="relative"):
            CompletionRecord.from_dict(
                _minimal_payload(validation_record_path="\\etc\\passwd")
            )

    def test_rejects_parent_traversal(self):
        with pytest.raises(ValueError, match="'\\.\\.'"):
            CompletionRecord.from_dict(
                _minimal_payload(
                    validation_record_path="../../other_worktree/secret.json"
                )
            )

    def test_rejects_parent_traversal_mid_path(self):
        with pytest.raises(ValueError, match="'\\.\\.'"):
            CompletionRecord.from_dict(
                _minimal_payload(
                    validation_record_path=".issue-orchestrator/../etc/passwd"
                )
            )

    def test_rejects_backslash_traversal(self):
        with pytest.raises(ValueError, match="'\\.\\.'"):
            CompletionRecord.from_dict(
                _minimal_payload(
                    validation_record_path="..\\..\\other\\secret.json"
                )
            )

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="null bytes"):
            CompletionRecord.from_dict(
                _minimal_payload(
                    validation_record_path=".issue-orchestrator/a\x00b.json"
                )
            )

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="non-empty"):
            CompletionRecord.from_dict(
                _minimal_payload(validation_record_path="   ")
            )

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="must be a string"):
            CompletionRecord.from_dict(
                _minimal_payload(validation_record_path=123)
            )

    def test_allows_none(self):
        rec = CompletionRecord.from_dict(_minimal_payload())
        assert rec.validation_record_path is None


class TestStringBounds:
    """F2 — agent-supplied string fields have size caps."""

    def test_comment_body_at_limit_ok(self):
        body = "a" * (64 * 1024)  # exactly 64 KB
        rec = CompletionRecord.from_dict(_minimal_payload(comment_body=body))
        assert rec.comment_body == body

    def test_comment_body_over_limit_rejected(self):
        body = "a" * (64 * 1024 + 1)
        with pytest.raises(ValueError, match="comment_body"):
            CompletionRecord.from_dict(_minimal_payload(comment_body=body))

    def test_review_issues_over_limit_rejected(self):
        body = "x" * (64 * 1024 + 1)
        with pytest.raises(ValueError, match="review_issues"):
            CompletionRecord.from_dict(_minimal_payload(review_issues=body))

    def test_summary_over_limit_rejected(self):
        huge = "s" * (64 * 1024 + 1)
        with pytest.raises(ValueError, match="summary"):
            CompletionRecord.from_dict(_minimal_payload(summary=huge))

    def test_implementation_over_limit_rejected(self):
        huge = "i" * (64 * 1024 + 1)
        with pytest.raises(ValueError, match="implementation"):
            CompletionRecord.from_dict(_minimal_payload(implementation=huge))

    def test_risk_level_tight_cap(self):
        with pytest.raises(ValueError, match="risk_level"):
            CompletionRecord.from_dict(
                _minimal_payload(risk_level="x" * 100)
            )

    def test_comment_body_rejects_null_byte(self):
        with pytest.raises(ValueError, match="null bytes"):
            CompletionRecord.from_dict(
                _minimal_payload(comment_body="before\x00after")
            )

    def test_comment_body_rejects_non_string(self):
        with pytest.raises(ValueError, match="must be a string"):
            CompletionRecord.from_dict(_minimal_payload(comment_body=42))


class TestPrLabelBounds:
    """F2 — pr_labels: count + per-item length + character allowlist."""

    def test_happy_path(self):
        rec = CompletionRecord.from_dict(
            _minimal_payload(pr_labels=["priority:high", "area/backend", "bug"])
        )
        assert rec.pr_labels == ["priority:high", "area/backend", "bug"]

    def test_rejects_too_many(self):
        labels = [f"label-{i}" for i in range(21)]
        with pytest.raises(ValueError, match="pr_labels exceeds"):
            CompletionRecord.from_dict(_minimal_payload(pr_labels=labels))

    def test_rejects_oversized_label(self):
        with pytest.raises(ValueError, match="pr_labels\\[0\\]"):
            CompletionRecord.from_dict(
                _minimal_payload(pr_labels=["a" * 100])
            )

    def test_rejects_html_injection_chars(self):
        with pytest.raises(ValueError, match="pr_labels\\[0\\]"):
            CompletionRecord.from_dict(
                _minimal_payload(pr_labels=["<script>alert(1)</script>"])
            )

    def test_rejects_leading_dash(self):
        with pytest.raises(ValueError, match="pr_labels\\[0\\]"):
            CompletionRecord.from_dict(_minimal_payload(pr_labels=["-bad"]))

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="pr_labels must be a list"):
            CompletionRecord.from_dict(_minimal_payload(pr_labels="bug"))

    def test_rejects_non_string_element(self):
        with pytest.raises(ValueError, match="pr_labels\\[0\\]"):
            CompletionRecord.from_dict(_minimal_payload(pr_labels=[42]))


class TestFollowUpIssuesBounds:
    """F7 — cap follow_up_issues list and per-item sizes."""

    def _follow_up(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "title": "Investigate flaky test",
            "reason": "Noticed intermittent failure in CI",
        }
        base.update(overrides)
        return base

    def test_happy_path(self):
        rec = CompletionRecord.from_dict(
            _minimal_payload(
                follow_up_issues=[self._follow_up(), self._follow_up()]
            )
        )
        assert rec.follow_up_issues is not None
        assert len(rec.follow_up_issues) == 2
        assert isinstance(rec.follow_up_issues[0], ProposedFollowUpIssue)

    def test_rejects_too_many(self):
        items = [self._follow_up() for _ in range(6)]
        with pytest.raises(ValueError, match="follow_up_issues exceeds"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=items)
            )

    def test_rejects_oversized_title(self):
        item = self._follow_up(title="t" * 300)
        with pytest.raises(ValueError, match="title"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )

    def test_rejects_oversized_reason(self):
        item = self._follow_up(reason="r" * (4 * 1024 + 1))
        with pytest.raises(ValueError, match="reason"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )

    def test_rejects_oversized_evidence(self):
        item = self._follow_up(evidence="e" * (4 * 1024 + 1))
        with pytest.raises(ValueError, match="evidence"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )

    def test_rejects_too_many_suggested_labels(self):
        item = self._follow_up(
            suggested_labels=[f"lbl-{i}" for i in range(11)]
        )
        with pytest.raises(ValueError, match="suggested_labels"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )

    def test_rejects_malformed_suggested_label(self):
        item = self._follow_up(suggested_labels=["<bad>"])
        with pytest.raises(ValueError, match="suggested_labels"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="follow_up_issues"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues={"title": "x"})
            )

    def test_rejects_null_byte_in_title(self):
        item = self._follow_up(title="bad\x00title")
        with pytest.raises(ValueError, match="null bytes"):
            CompletionRecord.from_dict(
                _minimal_payload(follow_up_issues=[item])
            )


class TestListBounds:
    """Count caps on misc list-of-strings fields."""

    def test_options_capped(self):
        too_many = [f"opt-{i}" for i in range(101)]
        with pytest.raises(ValueError, match="options exceeds"):
            CompletionRecord.from_dict(_minimal_payload(options=too_many))

    def test_blocked_by_capped(self):
        too_many = list(range(51))
        with pytest.raises(ValueError, match="blocked_by exceeds"):
            CompletionRecord.from_dict(_minimal_payload(blocked_by=too_many))

    def test_blocked_by_rejects_non_int(self):
        with pytest.raises(ValueError, match="blocked_by\\[0\\]"):
            CompletionRecord.from_dict(_minimal_payload(blocked_by=["123"]))

    def test_blocked_by_rejects_bool(self):
        with pytest.raises(ValueError, match="blocked_by\\[0\\]"):
            CompletionRecord.from_dict(_minimal_payload(blocked_by=[True]))


class TestRoundTrip:
    """Validation must not reject the orchestrator's own serialized output."""

    def test_happy_full_payload_roundtrips(self):
        payload = _minimal_payload(
            implementation="added validation",
            problems="none",
            comment_body="LGTM",
            pr_labels=["priority:high", "area/backend"],
            validation_record_path=".issue-orchestrator/validation-record.json",
            follow_up_issues=[
                {
                    "title": "Investigate flaky test",
                    "reason": "Intermittent failure",
                    "evidence": "logs show race",
                    "suggested_labels": ["flaky"],
                    "blocking": False,
                }
            ],
            options=["retry", "skip"],
            checks_passed=["unit", "lint"],
            blocked_by=[42, 43],
        )
        rec = CompletionRecord.from_dict(copy.deepcopy(payload))
        # Round-trip through to_dict and back
        rec2 = CompletionRecord.from_dict(rec.to_dict())
        assert rec2.pr_labels == rec.pr_labels
        assert rec2.validation_record_path == rec.validation_record_path
        assert rec2.follow_up_issues == rec.follow_up_issues
