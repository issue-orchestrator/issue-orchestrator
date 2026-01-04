"""Integration tests for gh_audit reporting."""

from __future__ import annotations

import json
import os

import importlib

def test_gh_audit_writes_report(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "gh-audit.json"
    monkeypatch.setenv("ORCHESTRATOR_GH_AUDIT", "1")
    monkeypatch.setenv("ORCHESTRATOR_GH_AUDIT_FILE", str(report_path))

    from issue_orchestrator.infra import gh_audit
    gh_audit = importlib.reload(gh_audit)

    gh_audit.record(
        args=["issue", "list"],
        repo="owner/repo",
        duration_ms=12,
        error=None,
        caller="test",
    )
    gh_audit.emit_report()

    data = json.loads(report_path.read_text())
    assert data["total_calls"] == 1
    assert data["by_command"].get("issue list") == 1


def test_gh_audit_usage_units_count_calls_and_items(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "gh-audit.json"
    monkeypatch.setenv("ORCHESTRATOR_GH_AUDIT", "1")
    monkeypatch.setenv("ORCHESTRATOR_GH_AUDIT_FILE", str(report_path))

    from issue_orchestrator.infra import gh_audit
    gh_audit = importlib.reload(gh_audit)

    with gh_audit.context(
        reason=gh_audit.AuditReason.SNAPSHOT_REFRESH,
        scope=gh_audit.AuditScope.MANUAL,
    ):
        gh_audit.record(
            args=["issue", "list"],
            repo="owner/repo",
            duration_ms=12,
            error=None,
            caller="test",
            items_returned=3,
        )

    gh_audit.emit_report()

    data = json.loads(report_path.read_text())
    assert data["total_calls"] == 1
    assert data["total_items_returned"] == 3
    assert data["usage_units"] == 4
    assert data["by_scope_totals"]["manual"]["usage_units"] == 4
    assert data["by_reason_totals"]["snapshot_refresh"]["usage_units"] == 4
    scope_reason = data["by_scope_reason"]["manual::snapshot_refresh"]
    assert scope_reason["usage_units"] == 4
