from __future__ import annotations

from issue_orchestrator.execution.review_exchange_transcript import (
    filter_review_exchange_transcript,
    parse_review_exchange_transcript,
    render_review_exchange_transcript,
)


def test_parse_filter_and_render_review_exchange_transcript_round_and_role_slice() -> None:
    transcript = (
        "[2026-03-20T17:13:16Z] round=1 role=reviewer section=prompt\n"
        "Reviewer prompt\n\n"
        "[2026-03-20T17:14:42Z] round=1 role=coder section=prompt\n"
        "Coder prompt\n\n"
        "[2026-03-20T17:16:47Z] round=2 role=reviewer section=completion\n"
        "response_type=ok text=Looks good.\n"
    )

    entries = parse_review_exchange_transcript(transcript)

    assert [entry.round_index for entry in entries] == [1, 1, 2]
    assert [entry.role for entry in entries] == ["reviewer", "coder", "reviewer"]

    round_two_reviewer = filter_review_exchange_transcript(
        entries,
        round_index=2,
        role="reviewer",
    )
    rendered = render_review_exchange_transcript(round_two_reviewer)

    assert "round=2 role=reviewer section=completion" in rendered
    assert "Looks good." in rendered
    assert "Coder prompt" not in rendered
    assert "round=1" not in rendered
