"""Canonical receipt identity is enforced before display or delivery trusts it."""

from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product

import pytest

from issue_orchestrator.domain.tech_lead_delivery import (
    DeliveryHistoryState,
    TechLeadDeliveryPolicy,
    TechLeadDeliveryStatus,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalBatchReviewScope,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
)
from issue_orchestrator.domain.tech_lead_run_record import (
    TechLeadDeliveryOutcome,
    TechLeadRunPhase,
    TechLeadRunReceipt,
    TechLeadRunRecord,
)
from issue_orchestrator.infra.sqlite_connection import open_sqlite
from issue_orchestrator.infra.tech_lead_run_record_store import (
    SqliteTechLeadRunRecordStore,
)

NOW = datetime(2026, 9, 6, 12)
SCOPES = (
    IssueInvestigationScope(1),
    GlobalHealthReviewScope(),
    GlobalBatchReviewScope(),
)


def completed_fields(scope):
    return dict(
        run_key=scope.run_key,
        scope_kind=scope.kind,
        flavor=scope.flavor,
        subject_issue_number=scope.subject_issue_number or 0,
        subject_title="Focus issue" if scope.subject_issue_number else "",
        anchor_issue_number=scope.subject_issue_number or 900,
        run_id="delivered",
        session_name="tech-lead",
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW - timedelta(hours=1),
        phase=TechLeadRunPhase.COMPLETED,
        delivery_outcome=TechLeadDeliveryOutcome.COMPLETED,
    )


# Each field is corrupted independently from a valid scope. Exercise every
# cross-scope key/kind/flavor pairing, mismatched issue subjects, noncanonical
# issue spellings, and both halves of the run identity with each blank shape.
CONTRADICTIONS = (
    [
        (scope, field, getattr(other, attribute))
        for scope, other in product(SCOPES, repeat=2)
        if scope != other
        for field, attribute in (
            ("run_key", "run_key"),
            ("scope_kind", "kind"),
            ("flavor", "flavor"),
        )
    ]
    + [
        (scope, "subject_issue_number", value)
        for scope in SCOPES
        for value in ((0, 2) if scope.subject_issue_number else (1,))
    ]
    + [(SCOPES[0], "run_key", value) for value in ("issue:2", "issue:01", "issue:١")]
    + [
        (scope, field, value)
        for scope, field, value in product(
            SCOPES, ("run_id", "session_name"), ("", "   ", "\t\r\n", "\u2003")
        )
    ]
)


@pytest.mark.parametrize("scope,field,value", CONTRADICTIONS)
@pytest.mark.parametrize("receipt_type", [TechLeadRunReceipt, TechLeadRunRecord])
def test_typed_receipts_refuse_contradictory_or_blank_identity(
    scope, field, value, receipt_type
):
    fields = completed_fields(scope)
    fields[field] = value
    with pytest.raises(ValueError):
        receipt_type(**fields)


@pytest.mark.parametrize("scope,field,value", CONTRADICTIONS)
def test_reopened_malformed_success_is_unknown_and_not_displayed(
    tmp_path, scope, field, value
):
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    valid = TechLeadRunRecord(**completed_fields(scope))
    for index, age in enumerate((8, 4, 0)):
        start = NOW - timedelta(hours=age)
        store.open_run(
            replace(
                valid,
                run_id=f"failed-{index}",
                started_at=start,
                ended_at=start,
                phase=TechLeadRunPhase.FAILED,
                delivery_outcome=TechLeadDeliveryOutcome.NOT_DELIVERED,
            )
        )
    assert (
        TechLeadDeliveryPolicy().evaluate(store.inspect_delivery_evidence(), now=NOW)
        is TechLeadDeliveryStatus.STALLED
    )
    store.open_run(valid)
    assert (
        TechLeadDeliveryPolicy().evaluate(store.inspect_delivery_evidence(), now=NOW)
        is TechLeadDeliveryStatus.OBSERVING
    )
    with open_sqlite(path) as connection:
        connection.execute(
            f"UPDATE tech_lead_run_records SET {field} = ? WHERE run_id = 'delivered'",
            (value,),
        )
    reopened = SqliteTechLeadRunRecordStore(path)
    assert {row.run_id for row in reopened.recent(limit=20)} == {
        "failed-0",
        "failed-1",
        "failed-2",
    }
    evidence = reopened.inspect_delivery_evidence()
    assert evidence.history_state is DeliveryHistoryState.INCOMPLETE
    assert evidence.last_delivered_at is None
    assert (
        TechLeadDeliveryPolicy().evaluate(evidence, now=NOW)
        is TechLeadDeliveryStatus.UNKNOWN
    )


@pytest.mark.parametrize("scope", (*SCOPES, IssueInvestigationScope(7391)))
@pytest.mark.parametrize("anchor", [0, 900])
def test_canonical_scopes_remain_valid_typed_and_reopened(tmp_path, scope, anchor):
    fields = completed_fields(scope)
    fields["anchor_issue_number"] = anchor
    # No normalization is applied to meaningful opaque run identity strings.
    fields["run_id"] = " retained-run "
    receipt = TechLeadRunReceipt(**fields)
    record = TechLeadRunRecord(**fields)
    path = tmp_path / "runs.sqlite"
    SqliteTechLeadRunRecordStore(path).open_run(record)
    reopened = SqliteTechLeadRunRecordStore(path)
    assert reopened.recent(limit=1) == (record,)
    evidence = reopened.inspect_delivery_evidence()
    assert receipt.delivered
    assert evidence.history_state is DeliveryHistoryState.COMPLETE
    assert evidence.last_delivered_at == NOW - timedelta(hours=1)
