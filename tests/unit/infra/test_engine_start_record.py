"""The engine-start record: written once per start, read back typed (#7490)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.engine_startup import record_engine_start
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.engine_start_record import (
    ENGINE_START_FILENAME,
    EngineStartRecordUnavailable,
    read_engine_start,
)
from issue_orchestrator.infra.repo_identity import state_dir

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def test_a_start_records_the_engine_commit_and_its_effective_charter(tmp_path: Path) -> None:
    config = Config()
    config.tech_lead.charter.flow.authority = "propose"
    config.tech_lead.findings.promote = "off"

    record_engine_start(config, repo_root=tmp_path, now=NOW)

    record = read_engine_start(state_dir(tmp_path))
    assert record.started_at == NOW
    assert record.engine_commit  # this test runs from a source checkout
    assert record.charter.roles["flow"].authority == "propose"
    assert record.charter.actions["create_issue"].outcome == "proposed"
    assert record.charter.promotion_lane == "off"
    assert "promote_finding" not in record.charter.actions


def test_a_later_start_replaces_the_record(tmp_path: Path) -> None:
    record_engine_start(Config(), repo_root=tmp_path, now=NOW)
    later = NOW.replace(hour=13)

    record_engine_start(Config(), repo_root=tmp_path, now=later)

    assert read_engine_start(state_dir(tmp_path)).started_at == later


def test_no_record_is_unavailable_not_guessed(tmp_path: Path) -> None:
    with pytest.raises(EngineStartRecordUnavailable, match="restart"):
        read_engine_start(tmp_path)


def test_a_record_that_is_not_the_contract_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / ENGINE_START_FILENAME).write_text(json.dumps({"started_at": "yesterday"}))

    with pytest.raises(EngineStartRecordUnavailable):
        read_engine_start(tmp_path)


def test_the_engine_records_its_start_when_it_takes_the_lock() -> None:
    """run_orchestrator calls the one writer right after acquiring the lock."""
    source = (
        Path(__file__).resolve().parents[3] / "src/issue_orchestrator/entrypoints/run_orchestrator.py"
    ).read_text(encoding="utf-8")
    lock = source.index("lock_info = acquire_lock(")
    record = source.index("record_engine_start(config, repo_root=repo_root")
    build = source.index("orchestrator = build_orchestrator(\n        config,")
    assert lock < record < build
