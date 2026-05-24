from __future__ import annotations

import pytest

from issue_orchestrator.domain.review_exchange_failures import (
    RoundFailureReason,
    round_failure_chapter_label,
    round_failure_narrative_phrase,
)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (RoundFailureReason.NO_COMPLETION, "did not complete"),
        (RoundFailureReason.TIMEOUT, "timed out"),
        (
            RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
            "exited before responding",
        ),
        (RoundFailureReason.INVALID_RESPONSE, "returned invalid response"),
        (
            RoundFailureReason.SESSION_CLOSED,
            "session was closed before responding",
        ),
        (RoundFailureReason.PROMPT_WRITE_FAILED, "prompt delivery failed"),
        (RoundFailureReason.ROUND_ERROR, "round failed"),
        (RoundFailureReason.UNKNOWN, "did not complete"),
    ],
)
def test_round_failure_reasons_share_presentation_vocabulary(
    reason: RoundFailureReason,
    expected: str,
) -> None:
    assert round_failure_chapter_label(reason) == expected
    assert round_failure_narrative_phrase(reason) == expected


def test_unknown_legacy_failure_reason_degrades_to_generic_no_completion() -> None:
    assert round_failure_chapter_label("future_reason") == "did not complete"
    assert round_failure_narrative_phrase("future_reason") == "did not complete"
