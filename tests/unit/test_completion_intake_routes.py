"""Typed producer to authenticated handler to durable receipt boundary."""

import pytest

from issue_orchestrator.entrypoints.cli_tools.completion_submit import (
    submission_payload,
)
from issue_orchestrator.entrypoints.control_api import control_app
from issue_orchestrator.entrypoints.control_api_issue_support import (
    ControlApiIssueDependencies,
    get_control_api_issue_dependencies,
)
from tests.unit.test_issue_run_evidence import run_record


@pytest.fixture
def intake_route_engine(monkeypatch, sample_orchestrator, tmp_path):
    deps = ControlApiIssueDependencies(
        get_orchestrator=lambda: sample_orchestrator, with_state_lock=lambda fn: fn()
    )
    monkeypatch.setitem(
        control_app.dependency_overrides,
        get_control_api_issue_dependencies,
        lambda: deps,
    )
    record = run_record(tmp_path / "allocated")
    ledger = sample_orchestrator.deps.issue_run_ledger
    ledger.record_run(42, record)
    token = ledger.submission_capability(record.run)
    candidate = tmp_path / "invalid.json"
    candidate.write_bytes(b"not valid JSON")
    return sample_orchestrator, record.run, token, candidate


def test_cli_payload_to_receipt_roundtrip_and_no_agent_selected_run(
    auth_enabled_control_client, fake_browser_auth, intake_route_engine
):
    engine, run, token, candidate = intake_route_engine
    payload = submission_payload(candidate).model_dump()
    assert submission_payload(candidate).model_dump() == payload
    headers = {
        "Authorization": "Bearer " + fake_browser_auth.agent_callback_token,
        "X-Completion-Capability": token,
    }
    response = auth_enabled_control_client.post(
        "/api/completion/submissions", json=payload, headers=headers
    )
    assert response.status_code == 200, response.text
    receipt = response.json()
    entry = engine.deps.issue_run_ledger.entries_for_run(run.identity)[0]
    assert receipt == {"entry_id": entry.entry_id, "content_sha256": entry.raw_sha256}
    assert entry.raw_path.read_bytes() == candidate.read_bytes()
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions", json=payload, headers=headers
        ).json()
        == receipt
    )
    for field in ("run_id", "run_dir", "manifest", "validation", "session_id"):
        assert (
            auth_enabled_control_client.post(
                "/api/completion/submissions",
                json={**payload, field: "forged"},
                headers=headers,
            ).status_code
            == 422
        )
    candidate.write_bytes(b'{"corrected": true}')
    corrected = submission_payload(candidate).model_dump()
    assert corrected["submission_key"] != payload["submission_key"]
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions", json=corrected, headers=headers
        ).status_code
        == 200
    )
    assert len(engine.deps.issue_run_ledger.entries_for_run(run.identity)) == 2
    engine.deps.completion_intake.close_and_drain(42)
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions",
            json={**payload, "submission_key": "late"},
            headers=headers,
        ).status_code
        == 409
    )


def test_run_capability_and_operator_auth_are_distinct(
    auth_enabled_control_client, fake_browser_auth, intake_route_engine
):
    engine, run, token, candidate = intake_route_engine
    payload = submission_payload(candidate).model_dump()
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions",
            json=payload,
            headers={"X-Completion-Capability": token},
        ).status_code
        == 401
    )
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions",
            json=payload,
            headers={
                **fake_browser_auth.bearer_headers(),
                "X-Completion-Capability": "wrong",
            },
        ).status_code
        == 401
    )
    assert (
        auth_enabled_control_client.post(
            "/api/validated-work/intake",
            json={},
            headers={
                "Authorization": "Bearer " + fake_browser_auth.agent_callback_token
            },
        ).status_code
        == 401
    )
    assert engine.deps.issue_run_ledger.entries_for_run(run.identity) == ()


def test_historical_operator_command_maps_exact_selection_and_typed_refusal(
    auth_enabled_control_client, fake_browser_auth, intake_route_engine
):
    engine, _, _, candidate = intake_route_engine
    command = {
        "repo_slug": "wrong/repo",
        "issue_number": 42,
        "branch_name": "feature",
        "target_head_sha": "a" * 40,
        "candidate_path": str(candidate),
        "candidate_sha256": "a" * 64,
        "actor": "operator",
        "reason": "selected sidecar",
    }
    response = auth_enabled_control_client.post(
        "/api/validated-work/intake",
        json=command,
        headers=fake_browser_auth.bearer_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "refused", "reason": "wrong_repository"}
    assert (
        auth_enabled_control_client.post(
            "/api/validated-work/intake",
            json={**command, "shell_command": "untrusted"},
            headers=fake_browser_auth.bearer_headers(),
        ).status_code
        == 422
    )


def test_historical_endpoint_returns_real_failed_validation_then_parked_admission(
    auth_enabled_control_client,
    fake_browser_auth,
    intake_route_engine,
    tmp_path,
    monkeypatch,
):
    from dataclasses import asdict, replace
    from unittest.mock import Mock
    from issue_orchestrator.control.completion_intake import (
        CompletionEvidenceIntakeService,
    )
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.ports.background_job import BackgroundJobRunner
    from issue_orchestrator.ports.command_runner import CommandResult
    from issue_orchestrator.ports.completion_intake import CompletionEvidenceValidator
    from tests.unit.test_historical_completion_intake import historical

    existing, _, _, _ = intake_route_engine
    owner, ledger, command, runner = historical(tmp_path / "selected")
    service = CompletionEvidenceIntakeService(
        ledger,
        Mock(spec=CompletionEvidenceValidator),
        owner,
        Mock(spec=BackgroundJobRunner),
    )
    engine = Orchestrator(
        existing.config, deps=replace(existing.deps, completion_intake=service), state=existing.state
    )
    deps = ControlApiIssueDependencies(
        get_orchestrator=lambda: engine, with_state_lock=lambda fn: fn()
    )
    monkeypatch.setitem(
        control_app.dependency_overrides,
        get_control_api_issue_dependencies,
        lambda: deps,
    )
    body = asdict(command)
    body["candidate_path"] = str(command.candidate_path)
    runner.run.return_value = CommandResult(
        returncode=1, stdout="failed", stderr="details", timed_out=False
    )
    failed = auth_enabled_control_client.post(
        "/api/validated-work/intake",
        json=body,
        headers=fake_browser_auth.bearer_headers(),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "validation_failed"
    runner.run.return_value = CommandResult(
        returncode=0, stdout="passed", stderr="", timed_out=False
    )
    parked = auth_enabled_control_client.post(
        "/api/validated-work/intake",
        json=body,
        headers=fake_browser_auth.bearer_headers(),
    )
    assert parked.status_code == 200, parked.text
    assert parked.json()["status"] == "parked"
    assert parked.json()["record_id"].startswith("r1:")
    assert parked.json()["evidence_id"].startswith("e1:")


def test_resume_requires_capability_bound_exact_receipt_and_ignores_canonical_file(
    auth_enabled_control_client, fake_browser_auth, intake_route_engine, monkeypatch
):
    import json
    from dataclasses import replace
    from unittest.mock import Mock
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.ports.working_copy import WorkingCopy
    from tests.run_allocation_helpers import make_completion_processor
    from tests.unit.test_completion_evidence_intake import completion

    existing, run, token, candidate = intake_route_engine
    run.worktree_path.mkdir(parents=True)
    raw = json.loads(completion())
    raw["requested_actions"] = []
    candidate.write_text(json.dumps(raw))
    headers = {
        "Authorization": "Bearer " + fake_browser_auth.agent_callback_token,
        "X-Completion-Capability": token,
    }
    receipt = auth_enabled_control_client.post(
        "/api/completion/submissions",
        json=submission_payload(candidate).model_dump(),
        headers=headers,
    ).json()
    wc = Mock(spec=WorkingCopy)
    wc.get_current_branch.return_value = "feature"
    wc.has_uncommitted_changes.return_value = False
    processor = make_completion_processor(
        completion_intake=existing.deps.completion_intake,
        session_output=existing.deps.session_output,
        agent_callback_endpoint=existing.deps.agent_callback_endpoint,
        label_adapter=existing.deps.repository_host,
        pr_adapter=existing.deps.repository_host,
        git_adapter=wc,
        config=existing.config,
        event_bus=None,
    )
    engine = Orchestrator(
        existing.config, deps=replace(existing.deps, completion_processor=processor), state=existing.state
    )
    deps = ControlApiIssueDependencies(
        get_orchestrator=lambda: engine, with_state_lock=lambda fn: fn()
    )
    monkeypatch.setitem(
        control_app.dependency_overrides,
        get_control_api_issue_dependencies,
        lambda: deps,
    )
    (run.worktree_path / "completion.json").write_bytes(b"forged canonical")
    assert (
        auth_enabled_control_client.post(
            "/api/issues/42/resume", json={"run_dir": str(run.run_dir)}, headers=headers
        ).status_code
        == 422
    )
    assert (
        auth_enabled_control_client.post(
            "/api/issues/43/resume", json=receipt, headers=headers
        ).status_code
        == 401
    )
    assert (
        auth_enabled_control_client.post(
            "/api/issues/42/resume",
            json=receipt,
            headers={**headers, "X-Completion-Capability": "wrong"},
        ).status_code
        == 401
    )
    result = auth_enabled_control_client.post(
        "/api/issues/42/resume", json=receipt, headers=headers
    )
    assert result.status_code == 200, result.text
    assert result.json()["success"] is True, result.text
    assert result.json()["actions_taken"] is None
    assert (
        existing.deps.issue_run_ledger.read_completion(receipt["entry_id"]).summary
        == raw["summary"]
    )
    existing.deps.completion_intake.close_and_drain(42)
    assert (
        auth_enabled_control_client.post(
            "/api/issues/42/resume", json=receipt, headers=headers
        ).status_code
        == 401
    )
    # Receipt lookup retry is allowed, but it does not reopen execution authority.
    assert (
        auth_enabled_control_client.post(
            "/api/completion/submissions",
            json=submission_payload(candidate).model_dump(),
            headers=headers,
        ).json()
        == receipt
    )


def test_actual_cli_lost_response_retries_same_durable_receipt(
    auth_enabled_control_client, fake_browser_auth, intake_route_engine, monkeypatch
):
    from io import BytesIO
    from urllib.error import URLError
    from urllib.parse import urlsplit
    from issue_orchestrator.entrypoints.cli_tools.completion_submit import (
        submit_completion_file,
    )

    engine, run, token, candidate = intake_route_engine
    original = candidate.read_bytes()
    calls = []

    class Transport:
        def open(self, request, timeout):
            response = auth_enabled_control_client.post(
                urlsplit(request.full_url).path,
                content=request.data,
                headers=dict(request.header_items()),
            )
            assert response.status_code == 200, response.text
            calls.append(response.json())
            if len(calls) == 1:
                raise URLError("simulated lost acknowledgement")
            return BytesIO(response.content)

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY", token)
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN",
        fake_browser_auth.agent_callback_token,
    )
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "8765")
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Transport())
    with pytest.raises(URLError):
        submit_completion_file(candidate)
    receipt = submit_completion_file(candidate)
    assert calls[0] == calls[1]
    assert receipt.entry_id == calls[0]["entry_id"]
    assert len(engine.deps.issue_run_ledger.entries_for_run(run.identity)) == 1
    assert candidate.read_bytes() == original
