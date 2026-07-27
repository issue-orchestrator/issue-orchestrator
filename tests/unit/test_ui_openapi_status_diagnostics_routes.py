"""Contract tests for the status + diagnostics UI OpenAPI route group (#6410).

Group 1 of ``docs/api/ui-openapi-migration-plan.md``. Each test drives the real
route through ``TestClient`` and validates the *wire* payload on both contract
layers — the JSON Schema in ``docs/api/ui-openapi.json`` and the generated
Pydantic model. A schema that only validates a hand-written literal proves
nothing; these assert the producer's own output conforms.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="jsonschema.RefResolver is deprecated",
)

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    ActiveSessionSummaryPayload,
    BlockedIssuesPayload,
    DebugSnapshotPayload,
    DependencyProblemsPayload,
    DoctorReportPayload,
    ExcludedIssuesPayload,
    OrchestratorInfoPayload,
    OrchestratorStatusPayload,
    RawConfigPayload,
    SessionFailureDiagnosisPayload,
    StaleIssuesPayload,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import DependencyProblem, PendingReview
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.audit import IssueAuditEntry, SkipReason
from issue_orchestrator.infra.doctor.types import Check, DoctorResult
from issue_orchestrator.infra.session_failure_diagnosis import SessionFailureDiagnosis
from tests.unit.test_web import (
    create_issue,
    create_mock_orchestrator,
    create_session,
)

SCHEMA_PATH = Path("docs/api/ui-openapi.json")

# (method, path) → component name. The quality guardrail enforces the same
# mapping from the route side; this pins it from the schema side so a
# regenerated contract cannot silently drop a route this group migrated.
GROUP_ONE_ROUTES: dict[tuple[str, str], str] = {
    ("get", "/api/status"): "OrchestratorStatusPayload",
    ("get", "/api/excluded-issues"): "ExcludedIssuesPayload",
    ("get", "/api/info"): "OrchestratorInfoPayload",
    ("get", "/api/config"): "RawConfigPayload",
    ("get", "/api/debug"): "DebugSnapshotPayload",
    ("get", "/api/doctor"): "DoctorReportPayload",
    ("get", "/api/blocked-issues"): "BlockedIssuesPayload",
    ("get", "/api/dependency-problems"): "DependencyProblemsPayload",
    ("get", "/api/stale-issues"): "StaleIssuesPayload",
    ("get", "/api/failure-diagnosis/{issue_number}"): "SessionFailureDiagnosisPayload",
    ("post", "/api/issues/{issue_number}/audit"): "SessionFailureDiagnosisPayload",
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validator(component: str) -> Draft202012Validator:
    data = _schema()
    resolver = RefResolver.from_schema(data)
    return Draft202012Validator(
        data["components"]["schemas"][component], resolver=resolver
    )


def _assert_conforms(payload: object, component: str, model: type[BaseModel]) -> None:
    """The payload must satisfy the JSON Schema *and* the generated model."""
    _validator(component).validate(payload)
    model.model_validate(payload)


def _get(path: str, orchestrator: object) -> dict:
    set_orchestrator(orchestrator)
    try:
        response = TestClient(app).get(path)
    finally:
        set_orchestrator(None)
    assert response.status_code == 200, response.text
    return response.json()


def _post(path: str, orchestrator: object) -> dict:
    set_orchestrator(orchestrator)
    try:
        response = TestClient(app).post(path)
    finally:
        set_orchestrator(None)
    assert response.status_code == 200, response.text
    return response.json()


def test_group_one_routes_are_declared_in_the_ui_openapi_contract() -> None:
    """Every migrated route has a ``paths`` entry whose 200 JSON body is a
    ``$ref`` to the expected component — the schema half of "contracted"."""
    paths = _schema()["paths"]

    for (method, path), component in GROUP_ONE_ROUTES.items():
        assert path in paths, f"{method.upper()} {path} missing from ui-openapi.json"
        operation = paths[path][method]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{component}"}, (
            f"{method.upper()} {path} must reference {component}"
        )


def test_status_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()
    issue = create_issue(41, "Active issue")
    mock_orch.state.active_sessions = [create_session(issue)]
    mock_orch.state.completed_today = [7]
    mock_orch.state.priority_queue = [41]
    mock_orch.state.pending_reviews = [
        PendingReview(
            issue_key=FakeIssueKey(name="41"),
            pr_number=100,
            pr_url="https://github.com/owner/repo/pull/100",
            branch_name="feature/41",
            _issue_number=41,
        )
    ]

    payload = _get("/api/status", mock_orch)

    _assert_conforms(payload, "OrchestratorStatusPayload", OrchestratorStatusPayload)
    assert payload["active_sessions"][0]["issue_number"] == 41
    assert payload["active_sessions"][0]["status"] == "running"
    assert payload["pending_reviews"][0]["pr_number"] == 100
    assert payload["startup_status"] == "complete"


def test_status_route_active_session_status_vocabulary_is_closed() -> None:
    """``status`` is a two-value enum on both layers. A third value (e.g. an
    ad hoc ``"stalled"``) must fail, not leak to the dashboard as a new
    unhandled state."""
    validator = _validator("ActiveSessionSummaryPayload")
    session = {
        "issue_number": 41,
        "title": "Active issue",
        "runtime_minutes": 3,
        "agent_type": "agent:web",
        "status": "slow",
        "branch": "feature/41",
    }
    _assert_conforms(session, "ActiveSessionSummaryPayload", ActiveSessionSummaryPayload)

    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**session, "status": "stalled"})
    with pytest.raises(ValidationError):
        ActiveSessionSummaryPayload.model_validate({**session, "status": "stalled"})


def test_status_route_rejects_unknown_startup_status() -> None:
    """``startup_status`` drives the Control Center's "Open dashboard" gate, so
    it is a closed enum rather than a free string on both layers."""
    payload = {
        "paused": False,
        "shutdown_requested": False,
        "startup_status": "settled",
        "active_sessions": [],
        "max_sessions": 3,
        "completed_today": [],
        "queue": [],
        "pending_reviews": [],
        "tick_id": None,
        "last_tick_time": None,
        "e2e_role": None,
    }

    with pytest.raises(JsonSchemaValidationError):
        _validator("OrchestratorStatusPayload").validate(payload)
    with pytest.raises(ValidationError):
        OrchestratorStatusPayload.model_validate(payload)


def test_excluded_issues_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()
    mock_orch.deps.label_manager = LabelManager(mock_orch.config)
    excluded_issue = create_issue(55, "Excluded issue", labels=["agent:web", "blocked"])
    mock_orch.state.dependency_problems = {
        55: DependencyProblem(
            issue_number=55,
            issue_title="Excluded issue",
            blocked_by=[(54, "Predecessor", "open")],
            summary="waiting on #54",
        )
    }

    with patch(
        "issue_orchestrator.entrypoints.web_status_routes.audit_queue",
        return_value=[
            IssueAuditEntry(
                issue=excluded_issue,
                status=SkipReason.BLOCKED,
                detail="blocked label",
            )
        ],
    ):
        payload = _get("/api/excluded-issues", mock_orch)

    _assert_conforms(payload, "ExcludedIssuesPayload", ExcludedIssuesPayload)
    entry = payload["excluded"][0]
    assert entry["issue_number"] == 55
    assert entry["flow_stage"] == "not_eligible"
    assert entry["excluded_reason"] == "dependency: waiting on #54"
    assert entry["flow_steps"][0] == {"key": "not_eligible", "label": "Not Eligible"}


def test_info_route_payload_matches_ui_openapi() -> None:
    payload = _get("/api/info", create_mock_orchestrator())

    _assert_conforms(payload, "OrchestratorInfoPayload", OrchestratorInfoPayload)
    assert payload["repo"] == "owner/repo"
    assert payload["max_sessions"] == 3
    assert set(payload["client_capabilities"]) == {
        "focus_session",
        "open_path",
        "reveal_worktree",
        "local_server_paths_only",
        "host_platform",
    }
    assert set(payload["repo_identity"]) == {
        "repo_root",
        "commit_sha",
        "branch",
        "working_tree_dirty",
        "dirty_fingerprint",
        "source_root",
    }


def test_config_route_payload_matches_ui_openapi(tmp_path: Path) -> None:
    mock_orch = create_mock_orchestrator()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("repo: owner/repo\n", encoding="utf-8")
    mock_orch.config.config_path = config_file

    payload = _get("/api/config", mock_orch)

    _assert_conforms(payload, "RawConfigPayload", RawConfigPayload)
    assert payload == {"config": "repo: owner/repo\n"}


def test_debug_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()
    mock_orch.state.priority_queue = [12, 13]

    payload = _get("/api/debug", mock_orch)

    _assert_conforms(payload, "DebugSnapshotPayload", DebugSnapshotPayload)
    assert payload["priority_queue"] == [12, 13]
    assert payload["agents"]["agent:web"]["timeout"] == 45
    assert payload["startup_options"]["filtering"] == {
        "label": None,
        "milestone": None,
        "milestones": [],
    }


def test_doctor_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()

    with patch(
        "issue_orchestrator.entrypoints.web_diagnostics_routes.run_doctor",
        return_value=DoctorResult([
            Check(name="Config", status="ok", detail="loaded"),
            Check(name="GitHub", status="warning", detail="rate limited"),
            Check(
                name="AI Gate",
                status="info",
                detail="last probe ok",
                expandable={"ran": True, "agents_tested": ["claude-code"]},
            ),
        ]),
    ):
        payload = _get("/api/doctor", mock_orch)

    _assert_conforms(payload, "DoctorReportPayload", DoctorReportPayload)
    assert payload["overall"] == "warning"
    # The orchestrator check is injected by the route, not by run_doctor.
    assert any(check["name"] == "Orchestrator" for check in payload["checks"])
    expandable = next(
        check for check in payload["checks"] if check["name"] == "AI Gate"
    )["expandable"]
    assert expandable["agents_tested"] == ["claude-code"]


def test_doctor_route_omits_expandable_for_ordinary_checks() -> None:
    """``DoctorResult.to_dict`` only emitted ``expandable`` when populated, and
    contracting the route must not start sending ``"expandable": null`` on every
    check. Asserted on the wire, because the model default is ``None`` and only
    ``response_model_exclude_none`` keeps the key out of the response body."""
    mock_orch = create_mock_orchestrator()

    with patch(
        "issue_orchestrator.entrypoints.web_diagnostics_routes.run_doctor",
        return_value=DoctorResult([
            Check(name="Config", status="ok", detail="loaded"),
            Check(
                name="AI Gate",
                status="info",
                detail="last probe ok",
                expandable={"ran": True, "agents_tested": ["claude-code"]},
            ),
        ]),
    ):
        payload = _get("/api/doctor", mock_orch)

    checks = {check["name"]: check for check in payload["checks"]}

    # Plain checks carry exactly the three always-present keys.
    assert "expandable" not in checks["Config"]
    assert set(checks["Config"]) == {"name", "status", "detail"}
    # The route-injected check is built without expandable data too.
    assert "expandable" not in checks["Orchestrator"]

    # Excluding None must not strip a populated expandable payload.
    assert checks["AI Gate"]["expandable"] == {
        "ran": True,
        "agents_tested": ["claude-code"],
    }
    _assert_conforms(payload, "DoctorReportPayload", DoctorReportPayload)


def test_doctor_route_runs_without_an_orchestrator() -> None:
    """Doctor stays available during startup failures, and the contract holds
    on that path too — the orchestrator check flips to ``error``."""
    set_orchestrator(None)
    with patch(
        "issue_orchestrator.entrypoints.web_diagnostics_routes.run_doctor",
        return_value=DoctorResult([Check(name="Config", status="ok", detail="loaded")]),
    ):
        response = TestClient(app).get("/api/doctor")

    assert response.status_code == 200
    payload = response.json()
    _assert_conforms(payload, "DoctorReportPayload", DoctorReportPayload)
    assert payload["overall"] == "error"


def test_doctor_report_rejects_unknown_check_status() -> None:
    """``status`` is the shared ``HealthStatus`` vocabulary; an unrecognised
    value must fail rather than render as an unstyled chip."""
    report = {
        "overall": "ok",
        "checks": [{"name": "Config", "status": "degraded", "detail": "loaded"}],
    }

    with pytest.raises(JsonSchemaValidationError):
        _validator("DoctorReportPayload").validate(report)
    with pytest.raises(ValidationError):
        DoctorReportPayload.model_validate(report)


def test_blocked_issues_route_payload_matches_ui_openapi(tmp_path: Path) -> None:
    mock_orch = create_mock_orchestrator()
    mock_orch.config.repo_root = tmp_path / "repo"
    mock_orch.config.worktree_base = tmp_path / "worktrees"
    mock_orch.deps.label_manager = LabelManager(mock_orch.config)
    mock_orch.deps.session_output = FileSystemSessionOutput()
    mock_orch.state.cached_queue_issues = [
        create_issue(123, "Blocked issue", labels=["agent:web", "blocked"])
    ]

    payload = _get("/api/blocked-issues", mock_orch)

    _assert_conforms(payload, "BlockedIssuesPayload", BlockedIssuesPayload)
    entry = payload["blocked_issues"][0]
    assert entry["issue_number"] == 123
    assert entry["blocking_label"] == "blocked"
    assert entry["has_completion"] is False
    assert entry["run_dir"] is None


def test_dependency_problems_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()
    mock_orch.state.dependency_problems = {
        77: DependencyProblem(
            issue_number=77,
            issue_title="Successor",
            blocked_by=[(76, "Predecessor", "open")],
            summary="waiting on #76",
        )
    }

    payload = _get("/api/dependency-problems", mock_orch)

    _assert_conforms(
        payload, "DependencyProblemsPayload", DependencyProblemsPayload
    )
    # JSON object keys are strings on the wire; the contract says so rather
    # than relying on silent int→str coercion.
    assert list(payload["problems"]) == ["77"]
    assert payload["problems"]["77"]["issue_url"] == (
        "https://github.com/owner/repo/issues/77"
    )


def test_stale_issues_route_payload_matches_ui_openapi() -> None:
    mock_orch = create_mock_orchestrator()
    mock_orch.config.stale_escalation_ticks = 3
    mock_orch.state.stale_issue_ticks = {88: 4, 89: 1}

    payload = _get("/api/stale-issues", mock_orch)

    _assert_conforms(payload, "StaleIssuesPayload", StaleIssuesPayload)
    assert payload["stale"]["88"]["persistent"] is True
    assert payload["stale"]["89"]["persistent"] is False
    assert payload["stale"]["88"]["threshold"] == 3


def _diagnosis_payload(**overrides: object) -> dict:
    """Built by the real producer so the contract is checked against its output."""
    return SessionFailureDiagnosis(
        issue_number=4057,
        ai_system="claude",
        permission_mode="bypassPermissions",
        worktree_path="/tmp/worktrees/repo-4057",
        log_path="/tmp/worktrees/repo-4057/session.log",
        log_exists=True,
        log_context="tail of the session log",
        history_status="failed",
        history_reason="validation failed",
        warnings=["worktree still present"],
        suggestions=["re-run validation"],
        review_feedback=[
            {"cycle": 1, "path": "/tmp/review-feedback/cycle-1.md", "content": "nit"}
        ],
        **overrides,  # type: ignore[arg-type]
    ).to_dict()


@pytest.mark.parametrize("method", ["get", "post"])
def test_failure_diagnosis_and_audit_share_one_contract(method: str) -> None:
    """``GET /api/failure-diagnosis/{n}`` and ``POST /api/issues/{n}/audit``
    return the same producer output, so they share one component rather than
    drifting into two shapes for the same data."""
    mock_orch = create_mock_orchestrator()
    mock_orch.get_failure_diagnosis = MagicMock(return_value=_diagnosis_payload())

    if method == "get":
        payload = _get("/api/failure-diagnosis/4057", mock_orch)
    else:
        payload = _post("/api/issues/4057/audit", mock_orch)

    _assert_conforms(
        payload, "SessionFailureDiagnosisPayload", SessionFailureDiagnosisPayload
    )
    assert payload["issue_number"] == 4057
    assert payload["review_feedback"][0]["cycle"] == 1
    mock_orch.get_failure_diagnosis.assert_called_once_with(4057)


def test_failure_diagnosis_analysis_block_is_always_present() -> None:
    """The analysis block used to appear only when a headline existed, so the
    wire shape varied per response. It is now always present — ``null`` means
    "no analysis recorded", which is a value the client can branch on."""
    mock_orch = create_mock_orchestrator()
    mock_orch.get_failure_diagnosis = MagicMock(return_value=_diagnosis_payload())

    without_analysis = _get("/api/failure-diagnosis/4057", mock_orch)

    _assert_conforms(
        without_analysis,
        "SessionFailureDiagnosisPayload",
        SessionFailureDiagnosisPayload,
    )
    assert without_analysis["analysis_headline"] is None
    assert without_analysis["analysis_detail"] is None
    assert without_analysis["analysis_suggestions"] == []

    mock_orch.get_failure_diagnosis = MagicMock(
        return_value=_diagnosis_payload(
            analysis_headline="Agent never committed",
            analysis_detail="No commits on the branch",
            analysis_suggestions=["check the pre-push hook"],
        )
    )

    with_analysis = _get("/api/failure-diagnosis/4057", mock_orch)

    _assert_conforms(
        with_analysis, "SessionFailureDiagnosisPayload", SessionFailureDiagnosisPayload
    )
    assert with_analysis["analysis_headline"] == "Agent never committed"
    assert with_analysis["analysis_suggestions"] == ["check the pre-push hook"]
    assert set(with_analysis) == set(without_analysis), (
        "the analysis block must not change which keys are on the wire"
    )


def test_failure_diagnosis_rejects_untyped_review_feedback() -> None:
    """``review_feedback`` used to be ``list[dict[str, Any]]`` all the way to
    the browser. It is now a typed cycle/path/content record on both layers."""
    payload = {
        **_diagnosis_payload(),
        "review_feedback": [{"cycle": 1, "unexpected": "field"}],
    }

    with pytest.raises(JsonSchemaValidationError):
        _validator("SessionFailureDiagnosisPayload").validate(payload)
    with pytest.raises(ValidationError):
        SessionFailureDiagnosisPayload.model_validate(payload)
