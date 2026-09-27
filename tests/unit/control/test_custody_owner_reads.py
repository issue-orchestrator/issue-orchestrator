"""The read-only questions custody asks other owners (#7331), and its wiring.

Each owner gained one read so custody never re-derives what that owner
decides: the shared needs-human block names its recorded causes, the provider
availability policy names the lanes holding an agent's work, and the host
rate-limit window reports how long it has held launches. The last test drives
the engine facade end to end, so a composition that forgot a collaborator
fails here rather than on the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from issue_orchestrator.control.needs_human_block import (
    NO_OTHER_NEEDS_HUMAN_CAUSES,
    NeedsHumanBlock,
)
from issue_orchestrator.domain.blocked_item_custody import CustodyState
from issue_orchestrator.domain.host_rate_limit import HostRateLimit, HostRateLimitWindow
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.provider_lane import BillingMode, ProviderLane
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


class _NoLabels:
    def add_label(self, issue_number: int, label: str) -> None:
        raise AssertionError("a display read must not write labels")

    def remove_label(self, issue_number: int, label: str) -> None:
        raise AssertionError("a display read must not write labels")


def _block(
    store: SqlitePendingWorkClaimStore, quarantined: Callable[[], frozenset[int]]
) -> NeedsHumanBlock:
    def no_fresh_read(_number: int) -> list[str]:
        raise AssertionError("a display read must not read labels from the host")

    return NeedsHumanBlock(
        needs_human_label="needs-human",
        labels=_NoLabels(),
        tech_lead_marker="tech-lead-needs-human",
        read_labels=no_fresh_read,
        quarantined_issue_numbers=quarantined,
        causes=store,
    )


def test_the_needs_human_block_names_its_recorded_causes(tmp_path: Path) -> None:
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    store.record_needs_human_cause(5, NeedsHumanCause.AGENT_COMPLETION.value, reason="asked")
    store.record_needs_human_cause(
        5, f"{NeedsHumanCause.VALIDATED_WORK_DISPOSITION.value}:record-1", reason="failed"
    )

    scans: list[int] = []

    def quarantined() -> frozenset[int]:
        scans.append(1)
        return frozenset({6})

    block = _block(store, quarantined=quarantined)

    assert block.recorded_causes([5, 6, 7]) == {
        5: frozenset(
            {NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.VALIDATED_WORK_DISPOSITION}
        ),
        6: frozenset({NeedsHumanCause.CLAIM_QUARANTINE}),
        7: frozenset(),
    }
    assert len(scans) == 1  # one quarantine scan for the whole board, not one per item
    assert NO_OTHER_NEEDS_HUMAN_CAUSES.recorded_causes([5]) == {5: frozenset()}


def test_an_unknown_recorded_cause_is_a_defect_not_a_silent_skip(tmp_path: Path) -> None:
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    store.record_needs_human_cause(8, "no_such_cause", reason="?")

    try:
        _block(store, quarantined=frozenset).recorded_causes([8])
    except ValueError as error:
        assert "no_such_cause" in str(error)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("an unknown cause key must raise")


def test_provider_availability_names_the_open_lanes_for_an_agent(sample_config) -> None:
    from tests.conftest import make_provider_availability

    sample_config.agents["agent:web"].provider = "codex"
    policy = make_provider_availability(sample_config)
    assert policy.open_lanes_for_agent("agent:web") == ()

    policy.provider_resilience.record_quota_failure(
        ProviderLane("codex", billing=BillingMode.METERED), error_summary="out of credit"
    )

    assert policy.open_lanes_for_agent("agent:web") == ("codex",)
    assert policy.open_lanes_for_agent(None) == ()
    assert policy.open_lanes_for_agent("agent:unknown") == ()
    issue = Issue(number=1, title="t", labels=["agent:web"])
    assert policy.circuit_is_open_for_issue(issue)


def test_the_rate_limit_window_dates_each_item_by_its_own_episode() -> None:
    window = HostRateLimitWindow()
    assert window.waiting_since("a") is None

    limit = HostRateLimit(resets_at=NOW + HOUR, kind="primary")
    window.observe(limit, NOW - 2 * HOUR, "a", live=frozenset({"a"}))
    window.observe(limit, NOW - HOUR, "b", live=frozenset({"a", "b"}))

    assert window.waiting_since("a") == NOW - 2 * HOUR
    assert window.waiting_since("b") == NOW - HOUR
    assert window.waiting_since("c") is None


def test_the_engine_facade_derives_custody_from_its_own_state(
    sample_orchestrator, make_session
) -> None:
    from dataclasses import replace

    orchestrator = sample_orchestrator
    tech_lead = orchestrator.config.tech_lead_review_agent = "agent:tech-lead"
    orchestrator.state.cached_scope_issues = [
        Issue(number=1, title="a", labels=["agent:web", "blocked-failed"]),
        Issue(number=2, title="b", labels=["agent:web", "blocked-failed"]),
    ]
    orchestrator.state.active_sessions.append(
        replace(make_session(issue_number=2), agent_label=tech_lead)
    )

    board = orchestrator.blocked_item_custody.read([1, 2])

    assert board.for_issue(1).state is CustodyState.UNOWNED
    assert board.for_issue(2).state is CustodyState.INVESTIGATING
    assert orchestrator.blocked_item_custody is orchestrator.blocked_item_custody
