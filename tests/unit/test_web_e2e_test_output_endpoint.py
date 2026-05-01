"""Endpoint tests for GET /api/e2e-run/{run_id}/test-output."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.infra.e2e_db import E2EDB
from issue_orchestrator.infra.e2e_reports import E2ERunArtifactRecord


def _orchestrator_for(repo_root: Path) -> MagicMock:
    mock_orch = MagicMock()
    mock_orch.config.repo_root = repo_root
    return mock_orch


def _start_run(db: E2EDB, repo_root: Path) -> int:
    return db.start_run(
        repo_root=str(repo_root),
        orchestrator_id="test-orch",
        pytest_args=["tests/e2e"],
        command=["pytest", "tests/e2e"],
        runner_kind="pytest",
    )


def _write_junit_with_output(path: Path) -> None:
    path.write_text(
        """\
<testsuite name="suite">
  <testcase classname="tests.e2e.test_smoke" name="test_chatty" time="0.10">
    <system-out>captured stdout content</system-out>
    <system-err>captured stderr content</system-err>
  </testcase>
  <testcase classname="tests.e2e.test_smoke" name="test_quiet" time="0.05" />
</testsuite>
""",
        encoding="utf-8",
    )


def test_returns_captured_output_for_known_nodeid() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)
        junit_path = repo_root / "junit.xml"
        _write_junit_with_output(junit_path)
        db.replace_run_artifacts(
            run_id,
            [
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML: junit.xml",
                    path=str(junit_path),
                )
            ],
        )

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            # Pytest path normalizes case_id to runtime nodeid; endpoint must
            # match the normalized form callers see in the UI.
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "tests/e2e/test_smoke.py::test_chatty"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["nodeid"] == "tests/e2e/test_smoke.py::test_chatty"
            assert body["system_out"] == "captured stdout content"
            assert body["system_err"] == "captured stderr content"
            assert body["source_path"].endswith("junit.xml")
        finally:
            set_orchestrator(None)


def test_returns_captured_output_for_raw_junit_case_id() -> None:
    """Command runner persists raw case_ids — the endpoint must match them too."""
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)
        junit_path = repo_root / "junit.xml"
        _write_junit_with_output(junit_path)
        db.replace_run_artifacts(
            run_id,
            [
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML: junit.xml",
                    path=str(junit_path),
                )
            ],
        )

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "tests.e2e.test_smoke::test_chatty"},
            )
            assert resp.status_code == 200
            assert resp.json()["system_out"] == "captured stdout content"
        finally:
            set_orchestrator(None)


def test_returns_404_when_test_has_no_captured_output() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)
        junit_path = repo_root / "junit.xml"
        _write_junit_with_output(junit_path)
        db.replace_run_artifacts(
            run_id,
            [
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML: junit.xml",
                    path=str(junit_path),
                )
            ],
        )

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "tests/e2e/test_smoke.py::test_quiet"},
            )
            assert resp.status_code == 404
            assert resp.json()["error"] == "not_found"
        finally:
            set_orchestrator(None)


def test_returns_404_when_nodeid_unknown_to_run() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)
        junit_path = repo_root / "junit.xml"
        _write_junit_with_output(junit_path)
        db.replace_run_artifacts(
            run_id,
            [
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML: junit.xml",
                    path=str(junit_path),
                )
            ],
        )

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "tests/e2e/test_smoke.py::does_not_exist"},
            )
            assert resp.status_code == 404
        finally:
            set_orchestrator(None)


def test_returns_404_when_run_has_no_junit_artifact() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "anything"},
            )
            assert resp.status_code == 404
            assert resp.json()["error"] == "no_junit"
        finally:
            set_orchestrator(None)


def test_returns_404_when_run_id_unknown() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                "/api/e2e-run/9999/test-output",
                params={"nodeid": "anything"},
            )
            assert resp.status_code == 404
            assert "E2E run 9999 not found" in resp.json()["detail"]
        finally:
            set_orchestrator(None)


def test_returns_400_when_nodeid_blank() -> None:
    with tempfile.TemporaryDirectory(prefix="e2e-test-output-") as tmp:
        repo_root = Path(tmp)
        (repo_root / ".issue-orchestrator").mkdir()
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = _start_run(db, repo_root)

        set_orchestrator(_orchestrator_for(repo_root))
        try:
            client = TestClient(app)
            resp = client.get(
                f"/api/e2e-run/{run_id}/test-output",
                params={"nodeid": "   "},
            )
            assert resp.status_code == 400
        finally:
            set_orchestrator(None)


def test_returns_503_when_orchestrator_unavailable() -> None:
    set_orchestrator(None)
    client = TestClient(app)
    resp = client.get("/api/e2e-run/1/test-output", params={"nodeid": "x"})
    assert resp.status_code == 503
