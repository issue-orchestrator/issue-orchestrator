"""The cold reads the improver's staging makes of the tech-lead stores (#7490)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    TechLeadCharterDecision,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.infra.tech_lead_run_record_store import SqliteTechLeadRunRecordStore


def _decision(action_id: str, at: str) -> TechLeadCharterDecision:
    return TechLeadCharterDecision.from_verdict(
        TechLeadCharterPolicy.from_config(Config()).decide("post_comment"),
        decision_id=f"d:{action_id}", source=CharterDecisionSource.DECISION, run_id="r",
        action_id=action_id, anchor_issue_number=1, target_number=1, target_is_pr=False,
        decided_at=at, tracks_proposal=False,
    )


def test_the_whole_ledger_reads_oldest_first(tmp_path: Path) -> None:
    store = SqliteTechLeadAuthorityStore(tmp_path / "a.sqlite")
    store.charter_ledger.record_decisions(
        [_decision("B", "2026-09-28T10:00:00+00:00"), _decision("A", "2026-09-27T10:00:00+00:00")]
    )

    assert [d.action_id for d in store.charter_ledger.list_all()] == ["A", "B"]


def test_case_files_read_with_their_diagnosis_and_every_observation(tmp_path: Path) -> None:
    store = SqliteTechLeadAuthorityStore(tmp_path / "a.sqlite")
    store.record_pattern(signature="sig", issue_number=372, observation_id="o1", diagnosis="why")
    store.note_pattern_observation(signature="sig", observation_id="o2")

    [record] = store.list_case_file_records()

    assert (record.signature, record.issue_number, record.diagnosis) == ("sig", 372, "why")
    assert [o.observation_id for o in record.observations] == ["o1", "o2"]
    assert record.observation_count == 2


def _run(run_id: str, started: datetime) -> TechLeadRunRecord:
    return TechLeadRunRecord(
        run_key="global:health_review", scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW, phase=TechLeadRunPhase.RUNNING,
        started_at=started, run_id=run_id, session_name="tl", anchor_issue_number=1,
    )


def test_the_run_history_counts_the_rows_it_cannot_read_back(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(path)
    store.open_run(_run("new", datetime(2026, 9, 28, 10)))
    store.open_run(_run("old", datetime(2026, 9, 27, 10)))
    store.open_run(_run("bad", datetime(2026, 9, 28, 11)))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tech_lead_run_records SET flavor = 'retired' WHERE run_id = 'bad'")

    history = store.all_runs()

    assert [r.run_id for r in history.records] == ["old", "new"]
    assert history.unreadable == 1
    assert history.records[0].started_at + timedelta(days=1) == history.records[1].started_at
