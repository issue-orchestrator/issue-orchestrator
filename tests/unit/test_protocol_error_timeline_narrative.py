"""Agent journey: when a coder hits a protocol error, the timeline
must surface the error in its narrative, not hide it under generic
"completed" wording.

Background
----------
``persistent_session_exchange._build_outcome_for_protocol_error``
emits two events when the coder violates its protocol contract
(e.g. it never wrote a completion artifact, or wrote one with a
malformed shape):

  - ``REVIEW_EXCHANGE_ROUND_COMPLETED`` with
    ``coder_response_type="protocol_error"`` and the error text in
    ``detail``.
  - ``REVIEW_EXCHANGE_COMPLETED`` with ``status="error"`` and
    ``reason="coder_protocol_error"``.

Before this PR, the fan-out narratives for these two events were:

  - round_completed: "Review round N completed — <reviewer_verdict>"
  - exchange_completed: "Review exchange completed (N rounds)"

Both phrasings hid the coder error. The user reading the dashboard
saw a benign-looking pair of "completed" rows and had to drill into
the artifacts (run_dir / exchange_dir / summary.json) to discover
that the agent actually died.

After this PR, the narratives must:

  - For ``coder_response_type == "protocol_error"`` round_completed,
    say "Review round N stopped — coder protocol error" instead of
    masking with the reviewer verdict.
  - For ``status != "ok"`` exchange_completed, say "Review exchange
    ended (<status>, N rounds) — <reason>" instead of "completed".

This pins what the user actually reads on the timeline, not just the
fact that events are emitted.
"""

from __future__ import annotations

from issue_orchestrator.events.fan_out_pipeline import produce_external_records
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import project_timeline


def _project_one(
    internal_event_name: str,
    data: dict,
    *,
    issue_number: int = 1234,
):
    """Helper: fan out one internal event and project to TimelineEvents."""
    records = list(produce_external_records(
        internal_event_name=internal_event_name,
        enriched_data=data,
        base_event_id="i-0001",
        timestamp_iso="2026-05-05T05:00:00+00:00",
    ))
    return project_timeline(records, issue_number=issue_number)


class TestRoundCompletedNarrativeOnProtocolError:
    """The user must see "coder protocol error" when the coder breaks
    its contract, not the irrelevant reviewer verdict from before
    the failure."""

    def test_protocol_error_surfaces_in_round_narrative(self) -> None:
        events = _project_one(
            "review_exchange.round_completed",
            {
                "issue_number": 1234,
                "session_name": "issue-1234",
                "round_index": 1,
                "reviewer_response_type": "changes_requested",
                "reviewer_response_text": "needs more tests",
                "coder_response_type": "protocol_error",
                "coder_response_text": None,
                "detail": "completion artifact missing required field 'outcome'",
            },
        )
        round_events = [
            e for e in events if e.event == "review_exchange.round_completed"
        ]
        assert len(round_events) == 1
        narrative = round_events[0].narrative
        assert narrative, "round_completed narrative is empty"
        assert "protocol error" in narrative.lower(), (
            "Round narrative does not surface the coder protocol error. "
            f"Narrative: {narrative!r}"
        )
        # And the reviewer verdict must NOT be the headline — the
        # coder error is the load-bearing fact for the user, not what
        # the prior reviewer said.
        assert "changes_requested" not in narrative.lower(), (
            "Round narrative leads with the reviewer verdict ("
            f"{narrative!r}); the coder protocol error is buried."
        )

    def test_normal_round_narrative_unchanged(self) -> None:
        """Regression guard: the normal happy/changes_requested path
        must keep its existing narrative shape so we don't break the
        rework-cycle journey assertions in PR #6207."""
        events = _project_one(
            "review_exchange.round_completed",
            {
                "issue_number": 1234,
                "session_name": "issue-1234",
                "round_index": 2,
                "reviewer_response_type": "ok",
                "reviewer_response_text": "looks good",
                "coder_response_type": "ok",
                "coder_response_text": "applied fixes",
            },
        )
        round_events = [
            e for e in events if e.event == "review_exchange.round_completed"
        ]
        assert len(round_events) == 1
        narrative = round_events[0].narrative
        assert narrative
        assert "round 2" in narrative.lower()
        assert "ok" in narrative.lower()
        assert "protocol error" not in narrative.lower()


class TestExchangeCompletedNarrativeOnError:
    """The user must see that the exchange failed when status != "ok",
    with the reason they can act on."""

    def test_protocol_error_status_surfaces_in_exchange_narrative(self) -> None:
        events = _project_one(
            "review_exchange.completed",
            {
                "issue_number": 1234,
                "session_name": "issue-1234",
                "rounds": 1,
                "status": "error",
                "reason": "coder_protocol_error",
                "detail": "completion artifact missing required field 'outcome'",
            },
        )
        completed_events = [
            e for e in events if e.event == "review_exchange.completed"
        ]
        assert len(completed_events) == 1
        narrative = completed_events[0].narrative
        assert narrative
        # Headline must signal the error, not "completed".
        assert "completed" not in narrative.lower() or "error" in narrative.lower(), (
            f"Error exchange narrative reads as success: {narrative!r}"
        )
        assert "error" in narrative.lower(), narrative
        # And the reason must be present so the user knows WHY.
        assert "coder_protocol_error" in narrative, (
            f"Exchange narrative on error path missing reason. Narrative: {narrative!r}"
        )

    def test_max_rounds_exceeded_surfaces_in_narrative(self) -> None:
        """The other non-ok terminal state — runner gave up after
        max_rounds — must also be visible in the narrative."""
        events = _project_one(
            "review_exchange.completed",
            {
                "issue_number": 1234,
                "session_name": "issue-1234",
                "rounds": 3,
                "status": "stopped",
                "reason": "max_rounds_exceeded",
            },
        )
        completed_events = [
            e for e in events if e.event == "review_exchange.completed"
        ]
        narrative = completed_events[0].narrative
        assert narrative
        assert "stopped" in narrative.lower() or "max_rounds_exceeded" in narrative
        assert "max_rounds_exceeded" in narrative

    def test_ok_exchange_narrative_unchanged(self) -> None:
        """Regression guard: the success path keeps "Review exchange
        completed (N rounds)" so dashboards / golden fixtures pinning
        that exact phrase don't drift."""
        events = _project_one(
            "review_exchange.completed",
            {
                "issue_number": 1234,
                "session_name": "issue-1234",
                "rounds": 2,
                "status": "ok",
                "reason": "reviewer_ok",
            },
        )
        completed_events = [
            e for e in events if e.event == "review_exchange.completed"
        ]
        narrative = completed_events[0].narrative
        assert narrative == "Review exchange completed (2 rounds)", (
            f"Success exchange narrative drifted: {narrative!r}"
        )
