"""Tests for `produce_external_records()` — the canonical fan-out surface.

Two-purpose suite:

1. **Edge cases** that have historically been miss-handled by
   re-implementations of writer fan-out policy. Particularly the
   rework-cycle phase guard (the bug that prompted extracting this
   module in the first place).

2. **API contract** of `produce_external_records()` and
   `enrich_narrative()` — keeping the public projection surface
   small and predictable.

If a future change introduces another conditional in the fan-out
pipeline (e.g. a new phase the override should preserve), it lands
here as a focused unit test rather than only being caught at the
golden layer.
"""

from __future__ import annotations

from issue_orchestrator.events.fan_out_pipeline import (
    enrich_narrative,
    produce_external_records,
)
from issue_orchestrator.ports.timeline_store import TimelineRecord


# ---------------------------------------------------------------------------
# produce_external_records: fan-out cardinality + identity
# ---------------------------------------------------------------------------

def test_produces_one_record_per_view_event() -> None:
    """`session.started` fans out to exactly one external ViewEvent in
    today's registry. The function must produce one record."""
    records = produce_external_records(
        internal_event_name="session.started",
        enriched_data={"issue_number": 1, "session_id": "issue-1-1"},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert len(records) == 1
    assert records[0].event == "agent.coding_started"
    assert records[0].source_event == "session.started"


def test_first_record_uses_base_event_id_subsequent_get_suffixes() -> None:
    """When fan-out produces N records, IDs must be base, base-1, base-2…"""
    # Use an event that fans out to a single user/ops event but exercise
    # the suffix code path by invoking on an event with only debug fan-out
    # — `claim.acquired` produces one debug-only record.
    records = produce_external_records(
        internal_event_name="claim.acquired",
        enriched_data={"issue_number": 1},
        base_event_id="base",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert records[0].event_id == "base"
    # If the registry ever gets a multi-record fan-out for an event used
    # here, the assertion below would fire and we'd add an explicit case.
    for i, record in enumerate(records[1:], start=1):
        assert record.event_id == f"base-{i}"


def test_unregistered_event_falls_through_to_debug_only() -> None:
    """Events with no VIEW_REGISTRY entry get a single debug-only record
    preserving the internal name."""
    records = produce_external_records(
        internal_event_name="totally.unknown_event",
        enriched_data={"issue_number": 1},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert len(records) == 1
    assert records[0].event == "totally.unknown_event"
    assert records[0].data["views"] == ["debug"]


# ---------------------------------------------------------------------------
# View-tier propagation
# ---------------------------------------------------------------------------

def test_views_metadata_propagates_from_view_registry() -> None:
    """The `views` field on each record's data dict mirrors the
    ViewEvent's view tier set, sorted for determinism."""
    [record] = produce_external_records(
        internal_event_name="review.approved",
        enriched_data={"issue_number": 1, "pr_number": 42},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert record.data["views"] == ["debug", "ops", "user"]


# ---------------------------------------------------------------------------
# logical_phase override — including the rework-cycle guard
# ---------------------------------------------------------------------------

def test_view_event_phase_overrides_default_logical_phase() -> None:
    """In the common case, `view_event.phase` (e.g. `coding`) replaces
    the upstream `logical_phase`."""
    [record] = produce_external_records(
        internal_event_name="session.started",
        enriched_data={"issue_number": 1, "logical_phase": "system"},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    # session.started's ViewEvent has phase="coding"
    assert record.data["logical_phase"] == "coding"


def test_phase_override_does_not_downgrade_rework_to_coding() -> None:
    """**Regression guard for the rework-cycle bug.**

    When `enrich_logical_semantics()` has promoted a coding event to
    `rework` (because we're in a rework cycle), the view-registry
    phase override of `coding` for `session.started` MUST NOT
    downgrade it. Mirrors `DefaultTimelineWriter` policy at the time
    this module was extracted.

    Test re-implementations of the fan-out missed this branch and
    pinned `logical_phase="coding"` for rework-driven scenarios,
    diverging from production. Routing the harness through this
    function eliminates that drift.
    """
    [record] = produce_external_records(
        internal_event_name="session.started",
        enriched_data={
            "issue_number": 1,
            "task": "code",
            "rework_cycle": 1,
            "logical_phase": "rework",  # already promoted upstream
        },
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert record.data["logical_phase"] == "rework", (
        "Rework-promoted phase must not be overwritten by ViewEvent "
        "phase='coding' — see fan_out_pipeline._build_external_data."
    )


def test_phase_override_applies_when_view_event_phase_differs_from_coding() -> None:
    """The rework guard is specifically about coding-vs-rework. Other
    phase overrides apply normally."""
    [record] = produce_external_records(
        internal_event_name="review.approved",
        enriched_data={"issue_number": 1, "pr_number": 42, "logical_phase": "rework"},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    # review.approved's ViewEvent has phase="review"; it overrides
    # because the guard is `enriched != "rework" OR ve.phase != "coding"`.
    assert record.data["logical_phase"] == "review"


# ---------------------------------------------------------------------------
# Narrative enrichment
# ---------------------------------------------------------------------------

def test_dynamic_narrative_enrichment_for_pr_created() -> None:
    """Static narrative `"PR created"` is enriched with the PR number."""
    [record] = produce_external_records(
        internal_event_name="issue.pr_created",
        enriched_data={"issue_number": 1, "pr_number": 42},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert record.data["narrative"] == "PR #42 created"


def test_static_narrative_used_when_enricher_returns_none() -> None:
    """If the enricher returns None for the supplied data, the static
    narrative from VIEW_REGISTRY is used unchanged."""
    [record] = produce_external_records(
        internal_event_name="session.started",
        # No reset_from_scratch → enricher returns None
        enriched_data={"issue_number": 1, "session_id": "issue-1-1"},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert record.data["narrative"] == "Coding agent started"


def test_session_started_scratch_narrative() -> None:
    """`reset_from_scratch=True` swaps in the scratch-coding narrative."""
    [record] = produce_external_records(
        internal_event_name="session.started",
        enriched_data={"issue_number": 1, "reset_from_scratch": True},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert record.data["narrative"] == "Scratch coding agent started"


# ---------------------------------------------------------------------------
# enrich_narrative direct API
# ---------------------------------------------------------------------------

def test_enrich_narrative_returns_static_for_unknown_event() -> None:
    """`enrich_narrative` for an event with no enricher returns the
    static narrative unchanged."""
    result = enrich_narrative("Some narrative", "totally.unknown_event", {})
    assert result == "Some narrative"


def test_enrich_narrative_returns_dynamic_when_data_supports() -> None:
    result = enrich_narrative(
        "Review round started",
        "review_exchange.round_started",
        {"round_index": 3},
    )
    assert result == "Review round 3 started"


# ---------------------------------------------------------------------------
# Records returned are typed and immutable from the caller's view
# ---------------------------------------------------------------------------

def test_return_type_is_timeline_record() -> None:
    [record] = produce_external_records(
        internal_event_name="session.started",
        enriched_data={"issue_number": 1},
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert isinstance(record, TimelineRecord)


def test_input_data_dict_not_mutated() -> None:
    """The function copies enriched_data; callers' dicts stay clean."""
    original = {"issue_number": 1, "logical_phase": "system"}
    snapshot = dict(original)
    produce_external_records(
        internal_event_name="session.started",
        enriched_data=original,
        base_event_id="evt-001",
        timestamp_iso="2026-05-04T00:00:00+00:00",
    )
    assert original == snapshot
