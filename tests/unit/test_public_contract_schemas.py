"""Ensure public contract schemas are generated and kept in sync."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.contracts.public import (
    TimelineEventContract,
    TimelineIssueContract,
    generate_public_schemas,
)
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import TimelineStream


def test_public_contract_schemas_are_current():
    base_dir = Path(__file__).resolve().parents[2]
    schema_dir = base_dir / "contracts" / "public"
    file_map = {
        path.stem: json.loads(path.read_text())
        for path in schema_dir.glob("*.json")
    }

    generated = generate_public_schemas()
    assert set(file_map) == set(generated)

    for name, schema in generated.items():
        assert file_map[name] == schema


def test_role_feedback_response_type_is_a_declared_public_timeline_field():
    # Issue #6428: the per-role verdict on ``review_exchange.role_feedback``
    # rides on ``response_type``, which the in-round Story progress projection
    # reads. It must be a *declared* field on the public timeline contract — not
    # silently tolerated as a permissive extra — and must round-trip through the
    # real producer (``TimelineStream`` -> ``to_dict``) into the contract.
    assert "response_type" in TimelineEventContract.model_fields

    record = TimelineRecord(
        event_id="review_exchange.role_feedback-reviewer-1",
        timestamp="2026-03-22T13:36:00Z",
        event="review_exchange.role_feedback",
        data={
            "issue_number": 4057,
            "round_index": 1,
            "role": "reviewer",
            "response_type": "changes_requested",
        },
    )
    payload = TimelineStream.from_records(4057, [record]).to_dict()

    issue = TimelineIssueContract.model_validate(payload)
    feedback = next(
        event for event in issue.events
        if event.event == "review_exchange.role_feedback"
    )
    assert feedback.response_type == "changes_requested"


def test_generated_public_timeline_schema_documents_response_type():
    # The on-disk public schema (the durable UI/test contract) must document the
    # field, so consumers of ``/api/timeline/{issue_number}`` can rely on it.
    schema = generate_public_schemas()["timeline.issue"]
    event_schema = schema["$defs"]["TimelineEventContract"]
    assert "response_type" in event_schema["properties"]
