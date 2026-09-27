"""Blocked-item custody on the dashboard payload (#7331): producer -> payload.

The custody owner decides; this boundary carries its answer onto every
blocked card and row, and the "is it under control?" summary onto the Blocked
column, in the generated contract's shape. The payload -> rendered-output half
is the UI's own test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest
from pydantic import ValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    BlockedCustodySummaryPayload,
    BlockedItemCustodyPayload,
    DashboardViewModelPayload,
)
from issue_orchestrator.control.blocked_item_custody_reader import (
    StateBlockedItemCustodyReader,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.blocked_item_custody import (
    BlockedCustodyBoard,
    BlockedItemCustody,
    CustodyCharterBasis,
    CustodyClock,
    CustodyState,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.blocked_item_custody import NO_ACTION_LIVENESS_OWNER
from issue_orchestrator.ports.provider_resilience import (
    NO_PROVIDER_CIRCUIT_STATUS,
    StaticProviderCircuitStatusReader,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.ports.tech_lead_run_record_store import NO_TECH_LEAD_RUN_HISTORY
from issue_orchestrator.view_models.blocked_custody import (
    custody_payload,
    custody_signal,
    custody_summary_payload,
)
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dashboard_flow import compute_compact_card_fingerprint

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _custody(number: int, state: CustodyState, **overrides: object) -> BlockedItemCustody:
    fields: dict[str, object] = {
        "issue_number": number,
        "state": state,
        "reason": f"reason {number}",
        "clock": CustodyClock(since=NOW - 3 * HOUR, basis="session started"),
        "stale_after": None if state is CustodyState.UNOWNED else 2 * HOUR,
        "stale": False,
    }
    fields.update(overrides)
    return BlockedItemCustody(**fields)  # type: ignore[arg-type]


BASIS = CustodyCharterBasis(
    decision_id="decision:run-1:A1",
    run_id="run-1",
    action_kind="kill_hung_session",
    role="flow",
    required_depth="workaround",
    role_enabled=True,
    role_depth="restructure",
    role_authority="execute",
    action_ceiling="propose",
    ceiling_source="tech_lead.authority.kill_hung_session",
    outcome="proposed",
    reason_code="action_authority_propose",
    reason="flow may act, but kill_hung_session is set to propose",
    decided_at=(NOW - HOUR).isoformat(),
    lifecycle="awaiting_approval",
    proposal_issue_number=700,
)


# -- one item ---------------------------------------------------------------------


def test_a_custody_payload_is_the_contract_s_shape_and_says_everything_in_words() -> None:
    custody = _custody(1, CustodyState.WAITING_ON_YOU, stale=True, charter=BASIS)

    payload = custody_payload(custody, NOW).model_dump(mode="json")

    BlockedItemCustodyPayload.model_validate(payload)
    assert payload["label"] == "Waiting on you"
    assert payload["owner"] == "you"
    assert payload["tone"] == "attention"  # stale overrides the state's tone
    assert payload["age_label"] == "3h"
    assert payload["stale_after_label"] == "2h"
    assert payload["attention_text"] == "Needs attention: waiting on you longer than 2h."
    assert payload["charter"]["role"] == "flow"
    assert payload["charter"]["outcome_label"] == "Proposed, awaiting approval"
    assert payload["charter"]["lifecycle_label"] == "awaiting approval"
    assert payload["charter"]["proposal_issue_number"] == 700


def test_an_unowned_item_always_asks_for_attention() -> None:
    payload = custody_payload(_custody(2, CustodyState.UNOWNED), NOW).model_dump(mode="json")

    assert payload["needs_attention"] is True
    assert payload["tone"] == "attention"
    assert payload["attention_text"] == "Needs attention: nobody owns it."
    assert payload["stale_after_label"] == ""


def test_a_lower_bound_age_says_at_least_and_an_undated_one_says_unknown() -> None:
    bounded = _custody(
        3,
        CustodyState.HELD,
        clock=CustodyClock(since=NOW - 2 * HOUR, basis="last issue activity", lower_bound=True),
    )
    undated = _custody(4, CustodyState.BEING_FIXED, clock=None)

    assert custody_payload(bounded, NOW).model_dump(mode="json")["age_label"] == "at least 2h"
    assert custody_payload(undated, NOW).model_dump(mode="json")["age_label"] == "age unknown"
    assert custody_payload(undated, NOW).model_dump(mode="json")["since"] == ""


def test_the_contract_rejects_a_state_it_does_not_know() -> None:
    payload = custody_payload(_custody(5, CustodyState.HELD), NOW).model_dump(mode="json")
    payload["state"] = "limbo"

    with pytest.raises(ValidationError):
        BlockedItemCustodyPayload.model_validate(payload)


def test_the_card_signal_ignores_the_ticking_age_but_not_what_the_card_says() -> None:
    young = _custody(6, CustodyState.INVESTIGATING)
    older = _custody(
        6, CustodyState.INVESTIGATING, clock=CustodyClock(since=NOW - 9 * HOUR, basis="x")
    )
    stale = _custody(6, CustodyState.INVESTIGATING, stale=True)

    assert custody_signal(young) == custody_signal(older)
    assert custody_signal(young) != custody_signal(stale)
    assert custody_signal(young) != custody_signal(_custody(6, CustodyState.HELD))


# -- the column summary ---------------------------------------------------------------


def test_the_summary_counts_unowned_and_stale_across_every_blocked_item() -> None:
    board = BlockedCustodyBoard(
        items=(
            _custody(1, CustodyState.UNOWNED),
            _custody(2, CustodyState.HELD, stale=True),
            _custody(3, CustodyState.HELD),
            _custody(4, CustodyState.INVESTIGATING),
        )
    )

    summary = custody_summary_payload(board).model_dump(mode="json")

    BlockedCustodySummaryPayload.model_validate(summary)
    assert (summary["total"], summary["needs_attention"], summary["unowned"], summary["stale"]) == (4, 2, 1, 1)
    assert summary["headline"] == "2 of 4 blocked items need attention (1 unowned, 1 stale)."
    assert summary["by_state"] == [
        {"state": "unowned", "label": "Unowned", "count": 1},
        {"state": "investigating", "label": "Investigating", "count": 1},
        {"state": "held", "label": "Held", "count": 2},
    ]


@pytest.mark.parametrize(
    ("items", "headline"),
    [
        ((), "Nothing is blocked."),
        ((_custody(1, CustodyState.HELD),), "The 1 blocked item is owned and within its time limit."),
    ],
)
def test_a_board_under_control_says_so(items: tuple[BlockedItemCustody, ...], headline: str) -> None:
    summary = custody_summary_payload(BlockedCustodyBoard(items=items)).model_dump(mode="json")

    assert summary["needs_attention"] == 0
    assert summary["headline"] == headline


# -- producer -> dashboard payload ----------------------------------------------------------


@dataclass
class _Orchestrator:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


@dataclass
class _RecordingReader:
    """Answers from a fixed board and records what the projection asked."""

    board: BlockedCustodyBoard
    asked: list[tuple[int, ...]]

    def read(self, issue_numbers: Sequence[int]) -> BlockedCustodyBoard:
        self.asked.append(tuple(issue_numbers))
        return self.board


def _config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.e2e.enabled = False
    return config


def _dashboard(state: OrchestratorState, reader) -> object:  # type: ignore[no-untyped-def]
    return build_dashboard_view_model(
        _Orchestrator(state=state, config=_config()),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        blocked_custody=reader,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )


def _blocked_state(*numbers: int) -> OrchestratorState:
    return OrchestratorState(
        startup_status="complete",
        cached_scope_issues=[
            Issue(number=n, title=f"Blocked {n}", labels=["agent:web", "blocked-failed"])
            for n in numbers
        ],
    )


def test_every_blocked_item_and_card_carries_its_custody_and_the_column_its_summary() -> None:
    board = BlockedCustodyBoard(
        items=(_custody(7, CustodyState.UNOWNED), _custody(8, CustodyState.INVESTIGATING))
    )
    reader = _RecordingReader(board=board, asked=[])

    view_model = _dashboard(_blocked_state(7, 8), reader)

    assert reader.asked == [(7, 8)]  # the FINAL blocked lane, asked once
    by_number = {item["issue_number"]: item for item in view_model.blocked_items}  # type: ignore[attr-defined]
    assert by_number[7]["custody"]["state"] == "unowned"
    assert by_number[8]["custody"]["label"] == "Investigating"
    blocked = next(c for c in view_model.flow_columns if c["id"] == "blocked")  # type: ignore[attr-defined]
    assert blocked["custody_summary"]["needs_attention"] == 1
    cards = {card["issue_number"]: card for card in blocked["items"]}
    assert cards[8]["custody"]["state"] == "investigating"
    assert cards[8]["custody_signal"] == custody_signal(board.for_issue(8))
    others = [c for c in view_model.flow_columns if c["id"] != "blocked"]  # type: ignore[attr-defined]
    assert all("custody_summary" not in column for column in others)
    DashboardViewModelPayload.model_validate(view_model.to_dict())  # type: ignore[attr-defined]


def test_a_custody_change_rebuilds_the_card() -> None:
    def card_for(custody: BlockedItemCustody) -> dict[str, object]:
        reader = _RecordingReader(board=BlockedCustodyBoard(items=(custody,)), asked=[])
        view_model = _dashboard(_blocked_state(9), reader)
        blocked = next(c for c in view_model.flow_columns if c["id"] == "blocked")  # type: ignore[attr-defined]
        return blocked["items"][0]

    queued = card_for(_custody(9, CustodyState.QUEUED_FOR_TECH_LEAD))
    investigating = card_for(_custody(9, CustodyState.INVESTIGATING))

    assert compute_compact_card_fingerprint(queued) != compute_compact_card_fingerprint(investigating)
    assert queued["fingerprint"] != investigating["fingerprint"]


def test_the_engine_s_own_reader_feeds_the_dashboard_end_to_end() -> None:
    config = _config()
    state = _blocked_state(10)
    state.cached_scope_issues[0] = Issue(
        number=10, title="Needs you", labels=["agent:web", "needs-human", "tech-lead-needs-human"]
    )
    reader = StateBlockedItemCustodyReader(
        config=config,
        state=lambda: state,
        labels=LabelManager(config),
        authority=InMemoryTechLeadAuthorityStore(),
        needs_human_causes=lambda _n: frozenset(),
        provider_lanes=lambda _a: (),
        provider_circuits=StaticProviderCircuitStatusReader(),
        parked_actions=NO_ACTION_LIVENESS_OWNER,
        clock=lambda: NOW,
    )

    view_model = _dashboard(state, reader)

    item = view_model.blocked_items[0]  # type: ignore[attr-defined]
    assert item["custody"]["state"] == "waiting_on_you"
    assert item["custody"]["reason"] == "The tech lead escalated it to you."
