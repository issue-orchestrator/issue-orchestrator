"""Endpoint regression tests for artifact drill-downs in E2E run detail (#6593).

These exercise the full ``/api/e2e-run-detail/{run_id}`` path for a
command-backed run with a failed testcase and multiple ``e2e_run_artifacts``
records, proving collected artifacts reach the API payload and that the
artifact-collection diagnostic classifies the three configuration states.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from issue_orchestrator.domain.timeline_key import TimelineKey
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from issue_orchestrator.infra.config_models import E2EConfig
from issue_orchestrator.infra.e2e_db import E2EDB
from issue_orchestrator.infra.e2e_reports import E2ERunArtifactRecord
from issue_orchestrator.ports.timeline_store import TimelineRecord

COMMAND = ["sh", "scripts/run-issue-orchestrator-suite.sh"]
FAILED_NODEID = "tixmeup.e2e.smoke::runtime.verify_primary_search"


def _seed_timeline(store: SqliteTimelineStore, run_id: int) -> None:
    """Seed the minimal e2e timeline the run-detail route needs to render."""
    store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
    records = [
        TimelineRecord(
            event_id="evt-start",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started",
            data={"branch": "main", "e2e_run_id": run_id},
            source_event="e2e.run_started",
        ),
        TimelineRecord(
            event_id="evt-test",
            timestamp="2026-01-01T00:00:10Z",
            event="e2e.test_completed",
            data={
                "nodeid": FAILED_NODEID,
                "outcome": "failed",
                "duration_seconds": 1.0,
                "e2e_run_id": run_id,
            },
            source_event="e2e.test_completed",
        ),
        TimelineRecord(
            event_id="evt-finish",
            timestamp="2026-01-01T00:01:00Z",
            event="e2e.run_finished",
            data={"status": "failed", "duration_seconds": 60.0, "e2e_run_id": run_id},
            source_event="e2e.run_finished",
        ),
    ]
    for record in records:
        store.append(store_key, record)


def _command_backed_failed_run(
    db: E2EDB,
    repo_root: Path,
    *,
    artifacts: list[E2ERunArtifactRecord],
) -> int:
    """Persist a finished command-backed run with a failed testcase."""
    run_id = db.start_run(
        repo_root=str(repo_root),
        orchestrator_id="test-orch",
        pytest_args=[],
        command=COMMAND,
        runner_kind="command",
    )
    db.upsert_test_result(
        run_id,
        nodeid=FAILED_NODEID,
        outcome="failed",
        longrepr=(
            "Step 'runtime.verify_primary_search' exited with code 1. "
            "See the raw E2E output log and captured runtime artifacts."
        ),
        result_source="junit_xml",
        stdout_available=True,
    )
    log_path = repo_root / ".issue-orchestrator" / "e2e-results" / "run-e2e-suite.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("GET /search/events?q=starter\n{\"results\":[]}\n", encoding="utf-8")
    if artifacts:
        db.replace_run_artifacts(run_id, artifacts)
    db.finish_run(
        run_id,
        status="failed",
        exit_code=1,
        duration_seconds=60.0,
        log_path=str(log_path),
    )
    return run_id


def _orchestrator_for(repo_root: Path, store: SqliteTimelineStore, e2e: E2EConfig) -> MagicMock:
    mock_orch = MagicMock()
    mock_orch.config.repo_root = repo_root
    mock_orch.config.e2e = e2e
    mock_orch.deps.timeline_store = store
    return mock_orch


def _run_detail(repo_root: Path, e2e: E2EConfig, artifacts: list[E2ERunArtifactRecord]) -> dict:
    db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
    run_id = _command_backed_failed_run(db, repo_root, artifacts=artifacts)
    store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )
    _seed_timeline(store, run_id)

    set_orchestrator(_orchestrator_for(repo_root, store, e2e))
    try:
        client = TestClient(app)
        resp = client.get(f"/api/e2e-run-detail/{run_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()
    finally:
        set_orchestrator(None)


def test_collected_artifacts_surface_in_run_detail_payload() -> None:
    """A failed command-backed run exposes every collected artifact record."""
    with tempfile.TemporaryDirectory(prefix="e2e-artifacts-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        results = repo_root / ".issue-orchestrator" / "e2e-results" / "run_37"
        results.mkdir(parents=True)
        artifacts = [
            E2ERunArtifactRecord("junit_xml", "JUnit XML: tixmeup-e2e-smoke.xml", str(results / "tixmeup-e2e-smoke.xml")),
            E2ERunArtifactRecord("text_artifact", "Text Artifact: run-e2e-suite.log", str(results / "run-e2e-suite.log")),
            E2ERunArtifactRecord("text_artifact", "Text Artifact: tixmeup-e2e-app-a.log", str(results / "tixmeup-e2e-app-a.log")),
            E2ERunArtifactRecord("text_artifact", "Text Artifact: compose-services.log", str(results / "compose-services.log")),
            E2ERunArtifactRecord("text_artifact", "Text Artifact: tixmeup-e2e-smoke.summary.txt", str(results / "tixmeup-e2e-smoke.summary.txt")),
        ]
        e2e = E2EConfig(
            runner_kind="command",
            command=COMMAND,
            junit_xml_paths=[".issue-orchestrator/e2e-results/**/*.xml"],
            artifact_paths=[
                ".issue-orchestrator/e2e-results/**/*.log",
                ".issue-orchestrator/e2e-results/**/*.summary.txt",
            ],
        )

        payload = _run_detail(repo_root, e2e, artifacts)

        payload_labels = {artifact["label"] for artifact in payload["artifacts"]}
        # Every collected DB artifact plus the always-present raw output log.
        for expected in (
            "JUnit XML: tixmeup-e2e-smoke.xml",
            "Text Artifact: run-e2e-suite.log",
            "Text Artifact: tixmeup-e2e-app-a.log",
            "Text Artifact: compose-services.log",
            "Text Artifact: tixmeup-e2e-smoke.summary.txt",
            "Raw Output",
        ):
            assert expected in payload_labels

        report_kinds = {report["kind"] for report in payload["reports"]}
        assert "junit_xml" in report_kinds

        assert payload["artifact_diagnostic"] == {
            "state": "collected",
            "collected_count": 5,
            "configured_glob_count": 3,
        }


def test_diagnostic_reports_globs_matched_nothing() -> None:
    """Configured globs that collected nothing are called out, not hidden."""
    with tempfile.TemporaryDirectory(prefix="e2e-artifacts-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        e2e = E2EConfig(
            runner_kind="command",
            command=COMMAND,
            artifact_paths=[".issue-orchestrator/e2e-results/**/*.log"],
        )

        payload = _run_detail(repo_root, e2e, artifacts=[])

        assert payload["artifact_diagnostic"]["state"] == "globs_matched_nothing"
        assert payload["artifact_diagnostic"]["collected_count"] == 0
        assert payload["artifact_diagnostic"]["configured_glob_count"] == 1
        # The raw output log is still surfaced even with no config-driven files.
        assert any(a["label"] == "Raw Output" for a in payload["artifacts"])


def test_diagnostic_reports_not_configured() -> None:
    """A repo with no artifact globs is distinguished from a collection gap."""
    with tempfile.TemporaryDirectory(prefix="e2e-artifacts-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        e2e = E2EConfig(runner_kind="command", command=COMMAND)

        payload = _run_detail(repo_root, e2e, artifacts=[])

        assert payload["artifact_diagnostic"] == {
            "state": "not_configured",
            "collected_count": 0,
            "configured_glob_count": 0,
        }
