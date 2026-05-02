"""Timeline presentation for the role-level review-exchange events.

Covers narrative enrichment (timeline_writer) and per-event context resolution
(timeline_presentation) for ``review_exchange.role_prompted``,
``review_exchange.role_feedback``, and ``review_exchange.role_timeout``.
"""

from __future__ import annotations

from issue_orchestrator.entrypoints.timeline_presentation import (
    _agent_log_context_for_event,
    _review_transcript_context_for_event,
)
from issue_orchestrator.execution.timeline_writer import _enrich_narrative


class TestRoleEventNarrativeEnrichment:
    def test_role_prompted_narrative_includes_role_and_round(self) -> None:
        narrative = _enrich_narrative(
            "default", "review_exchange.role_prompted",
            {"role": "reviewer", "round_index": 2, "prompt_chars": 1234},
        )
        assert narrative == "Reviewer prompt sent (round 2)"

    def test_role_feedback_narrative_includes_verdict(self) -> None:
        narrative = _enrich_narrative(
            "default", "review_exchange.role_feedback",
            {"role": "coder", "round_index": 1, "response_type": "ok"},
        )
        assert narrative == "Coder feedback (round 1) — ok"

    def test_role_feedback_narrative_omits_verdict_suffix_when_missing(self) -> None:
        narrative = _enrich_narrative(
            "default", "review_exchange.role_feedback",
            {"role": "reviewer", "round_index": 3},
        )
        assert narrative == "Reviewer feedback (round 3)"

    def test_role_timeout_narrative_includes_role_and_round(self) -> None:
        narrative = _enrich_narrative(
            "default", "review_exchange.role_timeout",
            {"role": "coder", "round_index": 4, "reason": "no_completion"},
        )
        assert narrative == "Coder timed out (round 4)"

    def test_unknown_role_falls_back_to_default_narrative(self) -> None:
        narrative = _enrich_narrative(
            "fallback", "review_exchange.role_prompted",
            {"role": "auditor", "round_index": 1},
        )
        assert narrative == "fallback"


class TestRoleEventContextResolution:
    def test_transcript_context_routes_to_role_specific_transcript(self) -> None:
        ctx = _review_transcript_context_for_event(
            {"role": "reviewer", "round_index": 2},
            "review_exchange.role_prompted",
        )
        assert ctx == {"round_index": 2, "transcript_role": "reviewer"}

    def test_agent_log_context_routes_to_role_specific_recording(self) -> None:
        ctx = _agent_log_context_for_event(
            {"role": "coder", "round_index": 5},
            "review_exchange.role_feedback",
        )
        assert ctx == {"round_index": 5, "session_role": "coder"}

    def test_role_timeout_context_routes_to_role(self) -> None:
        ctx = _agent_log_context_for_event(
            {"role": "reviewer", "round_index": 1, "reason": "no_completion"},
            "review_exchange.role_timeout",
        )
        assert ctx == {"round_index": 1, "session_role": "reviewer"}

    def test_unknown_role_yields_empty_context(self) -> None:
        # Unknown roles must not pollute downstream actions with bad paths.
        assert _review_transcript_context_for_event(
            {"role": "auditor", "round_index": 1},
            "review_exchange.role_prompted",
        ) == {}
        assert _agent_log_context_for_event(
            {"role": "auditor", "round_index": 1},
            "review_exchange.role_prompted",
        ) == {}

    def test_missing_round_index_yields_empty_context(self) -> None:
        assert _review_transcript_context_for_event(
            {"role": "reviewer"},
            "review_exchange.role_prompted",
        ) == {}
        assert _agent_log_context_for_event(
            {"role": "coder"},
            "review_exchange.role_feedback",
        ) == {}
