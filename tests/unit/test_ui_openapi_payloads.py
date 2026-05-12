"""Validate UI payloads against the UI OpenAPI schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="jsonschema.RefResolver is deprecated",
)

import pytest
from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    E2ERunDetailPayload,
    E2ERunTimelinePayload,
    IssueDetailActionPayload,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)
from issue_orchestrator.view_models.issue_detail import build_issue_detail_view_model
from issue_orchestrator.view_models.lifecycle_semantics import (
    OutcomeBadge,
    AgentIdentity,
    CompletedCodingAttempt,
    CompletionRecordEvidence,
    DashboardIteration,
    DashboardTimelineContainer,
    E2ERunIteration,
    E2ERunLifecycle,
    E2ESuiteTimelineContainer,
    IssueCycle,
    IssueLifecycle,
    OpenCompletionRecordCommand,
    OpenValidationDetailsCommand,
    PassedE2ETestExecution,
    ReviewNotReached,
    SessionRecordingUnavailable,
    ShowEventDetailsCommand,
    TimelineSubject,
    ValidationPassed,
)


@dataclass
class _OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False



# OutcomeBadge constructor shim for tests (PR #6333): the
# projection layer owns tone classification, but tests construct
# IssueCycle/JourneyRun directly with bare label strings.  This
# helper wraps any label in the typed shape so the assertions
# stay focused on cycle/run shape, not tone bookkeeping.
def _ob(label: str, tone: str = "neutral") -> OutcomeBadge:
    """Test helper: wrap a bare outcome label in an OutcomeBadge.
    Tone defaults to neutral; tests that care about tone pass it
    explicitly."""
    return OutcomeBadge(label=label, tone=tone)  # type: ignore[arg-type]

def _make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 300
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=30,
    )


def _validator(component: str) -> Draft202012Validator:
    schema = Path("docs/api/ui-openapi.json").read_text()
    data = __import__("json").loads(schema)
    resolver = RefResolver.from_schema(data)
    return Draft202012Validator(data["components"]["schemas"][component], resolver=resolver)


def _schema_error_messages(errors: list[JsonSchemaValidationError]) -> str:
    messages: list[str] = []
    pending = list(errors)
    while pending:
        error = pending.pop()
        messages.append(error.message)
        pending.extend(error.context)
    return "\n".join(messages)


def _e2e_timeline_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": "event:e2e-test",
        "timestamp": "2026-04-21T11:00:00Z",
        "event": "e2e.test_completed",
        "issue_number": -88,
        "phase": "execution",
        "step": "test_completed",
        "status": "completed",
        "level": "info",
        "summary": "tests/e2e/test_example.py::test_passes: passed",
        "parent_key": "e2e-run-88",
        "detail": None,
        "run_id": None,
        "run_dir": None,
        "artifacts": [],
        "unsupported_schema": False,
        "review_oriented": False,
        "event_intent": "system",
        "nodeid": "tests/e2e/test_example.py::test_passes",
        "outcome": "passed",
    }
    event.update(overrides)
    return event


def _e2e_timeline_cycle(*events: dict[str, object]) -> dict[str, object]:
    return {
        "cycle": 1,
        "start": "2026-04-21T11:00:00Z",
        "end": "2026-04-21T11:02:00Z",
        "status": "completed",
        "phases": ["execution"],
        "events": list(events),
        "summary": "E2E execution",
    }


def test_dashboard_view_model_matches_ui_openapi() -> None:
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    session_key = SessionKey(issue=FakeIssueKey("12"), task=TaskKind.REVIEW)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="review-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        started_at=datetime.now() - timedelta(minutes=3),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="active",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    validator = _validator("DashboardViewModelPayload")
    validator.validate(view_model.to_dict())


def test_issue_item_open_run_command_validates_against_ui_openapi() -> None:
    """PR #6329 reviewer Blocker 2: ``open_run_command`` is a typed
    field on ``IssueItemPayload``, not an opaque extra.

    Before the fix, ``IssueItemPayload`` was ``additionalProperties: true``
    with no declared ``open_run_command`` schema — meaning the field
    was accepted as an extra regardless of its shape.  The reviewer
    pointed out that malformed payloads (e.g. ``run_id: 0`` which the
    Pydantic model rejects) flowed through silently.

    Now ``open_run_command`` is an explicit
    ``OpenE2ERunCommandPayload | null`` field.  Valid payloads pass;
    malformed ones (wrong ``kind``, missing ``run_id``, wrong types
    on ``expand_run_details``) fail the JSON-schema validation.
    """
    validator = _validator("IssueItemPayload")

    # Valid case: a well-formed open_run_command attaches to an
    # E2E issue item.
    valid_item = {
        "issue_number": "E2E-88",
        "title": "Run details",
        "status": "passed",
        "action": "details",
        "action_hint": "View run details",
        "is_e2e": True,
        "e2e_run_id": 88,
        "open_run_command": {
            "kind": "open_e2e_run",
            "label": "Open E2E Run",
            "run_id": 88,
            "expand_run_details": False,
        },
    }
    validator.validate(valid_item)  # must not raise

    # Null is allowed (non-E2E items don't carry the command).
    valid_item_no_command = {**valid_item, "open_run_command": None}
    validator.validate(valid_item_no_command)

    # Malformed: wrong kind discriminator (using a different command kind).
    bad_kind = {**valid_item}
    bad_kind["open_run_command"] = {
        "kind": "open_issue_timeline",
        "label": "X",
        "run_id": 88,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_kind)

    # Malformed: missing required ``run_id``.
    bad_missing_run_id = {**valid_item}
    bad_missing_run_id["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_missing_run_id)

    # Malformed: wrong type for ``expand_run_details``.
    bad_expand_type = {**valid_item}
    bad_expand_type["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 88,
        "expand_run_details": "yes",  # must be boolean
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_expand_type)

    # Malformed: ``run_id`` must be a positive integer (PR #6329
    # round-4 blocker — the OpenAPI schema must enforce the same
    # invariant the canonical ``OpenE2ERunCommand`` Pydantic model
    # enforces).
    bad_run_id_zero = {**valid_item}
    bad_run_id_zero["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 0,
        "expand_run_details": False,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_run_id_zero)

    bad_run_id_negative = {**valid_item}
    bad_run_id_negative["open_run_command"] = {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": -5,
        "expand_run_details": False,
    }
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(bad_run_id_negative)


def test_issue_item_open_run_command_pydantic_rejects_non_positive_run_id() -> None:
    """The GENERATED Pydantic contract must enforce the same
    ``run_id >= 1`` invariant the canonical model enforces.

    PR #6329 round-4 blocker: the generator was emitting
    ``run_id: int`` with no constraint, so
    ``IssueItemPayload.model_validate({...})`` silently accepted
    ``run_id: 0``.  After extending the generator to map
    ``minimum: 1`` → ``Field(..., ge=1)``, the generated contract
    enforces the same invariant at the Python boundary that JSON
    schema enforces at the validator boundary.
    """
    from issue_orchestrator.contracts.ui_openapi_models import IssueItemPayload
    from pydantic import ValidationError

    # Valid → succeeds.
    valid = IssueItemPayload.model_validate({
        "issue_number": "E2E-88",
        "open_run_command": {
            "kind": "open_e2e_run",
            "label": "Open E2E Run",
            "run_id": 88,
            "expand_run_details": False,
        },
    })
    assert valid.open_run_command is not None
    assert valid.open_run_command.run_id == 88

    # run_id=0 → rejected by the generated Pydantic model.
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        IssueItemPayload.model_validate({
            "issue_number": "E2E-88",
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": 0,
                "expand_run_details": False,
            },
        })

    # Negative run_id → also rejected.
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        IssueItemPayload.model_validate({
            "issue_number": "E2E-88",
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": -1,
                "expand_run_details": False,
            },
        })


def test_issue_item_open_run_command_strict_int_rejects_string_and_boolean() -> None:
    """The generated UI contract enforces strict-int scalar semantics
    on ``run_id`` — no coercion from strings or booleans.

    PR #6329 round-5 blocker: ``Pydantic``'s default ``int`` field
    accepts ``"88"`` (string) and ``True`` (boolean) by coercing
    them to ``88`` and ``1`` respectively.  JSON Schema's
    ``type: integer`` rejects both.  The generator now emits
    ``Field(..., ge=1, strict=True)`` for numeric-constrained
    integer fields so the Python contract matches the wire
    contract — malformed scalars fail loudly, not silently
    normalize.
    """
    from issue_orchestrator.contracts.ui_openapi_models import IssueItemPayload
    from pydantic import ValidationError

    # String ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate({
            "issue_number": "E2E-88",
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": "88",  # string — should not coerce
                "expand_run_details": False,
            },
        })

    # Boolean True ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate({
            "issue_number": "E2E-88",
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": True,  # bool — should not coerce to 1
                "expand_run_details": False,
            },
        })

    # Boolean False ``run_id`` → rejected.
    with pytest.raises(ValidationError):
        IssueItemPayload.model_validate({
            "issue_number": "E2E-88",
            "open_run_command": {
                "kind": "open_e2e_run",
                "label": "Open E2E Run",
                "run_id": False,
                "expand_run_details": False,
            },
        })


def test_open_inline_agent_attempts_command_payload_matches_openapi() -> None:
    """Issue #6322 follow-up: the inline ``▸ Attempts on issue #N``
    expander emits a typed ``OpenInlineAgentAttemptsCommandPayload``
    on its ``<details>`` element.  Both the OpenAPI schema and the
    generated Pydantic contract enforce the same invariants:

      * ``kind`` is the literal ``open_inline_agent_attempts``.
      * ``issue_number`` is a positive integer (``minimum: 1``).
      * Strict-int scalar semantics — no coercion from strings or
        booleans — same as ``OpenE2ERunCommand.run_id``.
    """
    from issue_orchestrator.contracts.ui_openapi_models import (
        OpenInlineAgentAttemptsCommandPayload,
    )
    from pydantic import ValidationError

    validator = _validator("OpenInlineAgentAttemptsCommandPayload")
    # Valid payload.
    valid = {
        "kind": "open_inline_agent_attempts",
        "label": "Open Inline Agent Attempts",
        "issue_number": 4503,
    }
    validator.validate(valid)
    OpenInlineAgentAttemptsCommandPayload.model_validate(valid)

    # Wrong kind discriminator.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "kind": "open_e2e_run"})

    # Non-positive issue numbers.
    with pytest.raises(JsonSchemaValidationError):
        validator.validate({**valid, "issue_number": 0})
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate({**valid, "issue_number": 0})
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate({**valid, "issue_number": -1})

    # Strict-int: reject string and boolean coercion at the Python layer.
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate({**valid, "issue_number": "4503"})
    with pytest.raises(ValidationError):
        OpenInlineAgentAttemptsCommandPayload.model_validate({**valid, "issue_number": True})


def test_dialog_payloads_match_ui_openapi() -> None:
    info = build_info_dialog({
        "version": "1.0",
        "repo": "test/repo",
        "ui_mode": "web",
        "terminal_backend": "subprocess",
        "commit_short": "abc123",
        "max_sessions": 2,
        "active_sessions": 1,
        "completed_today": 0,
    })
    _validator("InfoDialogPayload").validate(info)

    config_dialog = build_config_dialog("config: value")
    _validator("ConfigDialogPayload").validate(config_dialog)

    debug_dialog = build_debug_dialog({
        "startup_options": {"ui_mode": "web", "web_port": 8080, "test_mode": False, "filtering": {}},
        "paused": False,
        "priority_queue": [],
        "config_path": "/tmp/config.yaml",
        "repo_root": "/tmp/repo",
    })
    _validator("DebugDialogPayload").validate(debug_dialog)

    doctor_dialog = build_doctor_dialog({
        "overall": "ok",
        "checks": [{"name": "health", "status": "ok", "detail": "ok"}],
    })
    _validator("DoctorDialogPayload").validate(doctor_dialog)

    session_diag = build_session_diagnostics_dialog(42, {
        "manifest": {
            "session_name": "session-42",
            "started_at": "2024-01-01T00:00:00Z",
            "run_id": "run-1",
            "backend": "subprocess",
            "agent_label": "agent:web",
            "claude_session_id": "abc",
            "worktree": "/tmp/worktree",
            "follow_up_issues": [
                {
                    "title": "Create flaky test follow-up",
                    "reason": "A flaky test was discovered while validating the assigned issue.",
                    "blocking": False,
                }
            ],
        },
        "run_dir": "/tmp/run",
    })
    _validator("SessionDiagnosticsDialogPayload").validate(session_diag)

    blocked_dialog = build_blocked_issues_dialog({"blocked_issues": [{"issue": 1}]})
    _validator("BlockedIssuesDialogPayload").validate(blocked_dialog)

    phase_dialog = build_phase_dialog({"phases": [{"name": "review-1", "display_name": "Review"}]}, 12, None)
    _validator("PhaseDialogPayload").validate(phase_dialog)

    validation_dialog = build_validation_failure_dialog(
        42,
        {
            "manifest": {
                "session_name": "session-42",
                "worktree": "/tmp/worktree",
                "validation_record_path": ".issue-orchestrator/sessions/run-1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/run-1/validation-output.log",
                "validation_stderr": ".issue-orchestrator/sessions/run-1/validation-stderr.log",
            },
            "run_dir": "/tmp/run",
            "validation_failure": {
                "reason": "Validation failed for abc123 (exit_code=2)",
                "suite": "publish_gate",
                "command": "make validate-pr",
                "exit_code": 2,
                "started_at": "2026-04-24T00:00:00Z",
                "ended_at": "2026-04-24T00:01:00Z",
                "failed_tests": ["tests/unit/test_example.py::test_breaks"],
                "stdout_excerpt": ["FAILED tests/unit/test_example.py::test_breaks"],
                "stderr_excerpt": ["make: *** [validate-pr] Error 2"],
            },
        },
    )
    _validator("ValidationFailureDialogPayload").validate(validation_dialog)


def test_issue_detail_payload_matches_ui_openapi() -> None:
    payload = build_issue_detail_view_model(
        issue_number=12,
        title="Issue #12",
        issue_url="https://github.com/test/repo/issues/12",
        events=[{"event": "session.started", "status": "started"}],
        phase_toc=[{"phase": "in_progress", "label": "In Progress"}],
        cycles=[{"cycle": 1, "status": "started", "phases": ["in_progress"]}],
    )
    _validator("IssueDetailPayload").validate(payload)


def _journey_event(
    event: str,
    *,
    timestamp: str,
    logical_run: int,
    logical_cycle: int,
    logical_phase: str = "coding",
    status: str = "started",
    summary: str | None = None,
    run_dir: str | None = None,
    agent: str | None = None,
) -> dict[str, object]:
    """Minimal logical-cycle-annotated event suitable for the typed pipeline."""
    return {
        "event": event,
        "timestamp": timestamp,
        "status": status,
        "logical_run": logical_run,
        "logical_cycle": logical_cycle,
        "logical_phase": logical_phase,
        "summary": summary,
        "run_dir": run_dir,
        "agent": agent,
    }


def test_issue_detail_runs_payload_uses_typed_journey_run_shape() -> None:
    """``/api/issue-detail`` exposes ``runs[].cycles[]`` as typed journey
    cycles with the new ``CycleValidationBadge`` (issue #6310 AC-1).
    The wire shape conforms to ``JourneyRunPayload`` and
    ``IssueCyclePayload``; the typed badge carries an
    ``OpenValidationDetailsCommand`` when a validation event is recorded.
    """
    from issue_orchestrator.view_models.issue_detail import IssueStoryContext

    events = [
        _journey_event(
            "session.started",
            timestamp="2026-04-21T10:00:00Z",
            logical_run=1,
            logical_cycle=1,
            run_dir="/tmp/run-1",
            agent="agent:backend",
        ),
        _journey_event(
            "validation.passed",
            timestamp="2026-04-21T10:05:00Z",
            logical_run=1,
            logical_cycle=1,
            logical_phase="coding",
            run_dir="/tmp/run-1",
            status="completed",
        ),
        _journey_event(
            "session.completed",
            timestamp="2026-04-21T10:06:00Z",
            logical_run=1,
            logical_cycle=1,
            logical_phase="coding",
            status="completed",
        ),
    ]
    payload = build_issue_detail_view_model(
        issue_number=4124,
        title="Issue #4124",
        issue_url="https://github.com/test/repo/issues/4124",
        events=events,
        phase_toc=[],
        cycles=[],
        context=IssueStoryContext(flow_stage="in_progress"),
    )
    _validator("IssueDetailPayload").validate(payload)

    runs = payload["runs"]
    assert len(runs) == 1
    run = runs[0]
    # JourneyRun typed shape
    assert run["run_number"] == 1
    assert run["run_label"] == "Run 1"
    assert run["reset_from_scratch"] is False
    cycles = run["cycles"]
    assert len(cycles) == 1
    cycle = cycles[0]
    # IssueCycle journey-overlay fields are populated (not None) because the
    # journey pipeline ran with an ``IssueProjectionContext``.
    assert cycle["lifecycle"] == 1
    assert cycle["iteration"] == 1
    assert cycle["cycle_label"] == "Cycle 1"
    assert cycle["agent"] == "backend"
    assert isinstance(cycle["steps"], list) and cycle["steps"]
    assert isinstance(cycle["phase_groups"], list) and cycle["phase_groups"]
    # Typed CycleValidationBadge with the typed command
    badge = cycle["validation"]
    assert badge is not None
    assert badge["state"] == "passed"
    assert badge["command"] is not None
    assert badge["command"]["kind"] == "open_validation_details"
    assert badge["command"]["issue_number"] == 4124
    assert badge["command"]["run_dir"] == "/tmp/run-1"


def test_issue_detail_runs_payload_rejects_arbitrary_journey_run_dicts() -> None:
    """Untyped journey run dicts no longer satisfy ``IssueDetailPayload`` —
    the public drawer field is genuinely typed (issue #6310 AC-1)."""
    payload = build_issue_detail_view_model(
        issue_number=12,
        title="Issue #12",
        issue_url="https://github.com/test/repo/issues/12",
        events=[{"event": "session.started", "status": "started"}],
        phase_toc=[],
        cycles=[],
    )
    # Inject a typed-violating run; validation must reject.
    payload["runs"] = [{"not_a_journey_run": True}]
    errors = list(_validator("IssueDetailPayload").iter_errors(payload))
    assert errors, "untyped runs dict should fail schema validation"
    messages = _schema_error_messages(errors)
    assert (
        "not_a_journey_run" in messages
        or "run_number" in messages
        or "is a required property" in messages
    ), f"unexpected validation message: {messages}"


def test_e2e_linked_issue_lifecycle_cycles_leave_journey_fields_null() -> None:
    """E2E ``linked_issue_lifecycles[].cycles[]`` has no journey context —
    the typed ``IssueCycle`` reports journey fields as ``None``, not
    sentinel placeholders (issue #6310 AC-1 / Blocker 3)."""
    from issue_orchestrator.view_models.lifecycle_projection import (
        project_e2e_suite_lifecycle_container_for_run,
    )

    container = project_e2e_suite_lifecycle_container_for_run(
        run_id=88,
        events=[
            {
                "event": "e2e.run_started",
                "timestamp": "2026-04-21T11:00:00Z",
                "run_id": 88,
            },
            {
                "event": "e2e.test_started",
                "timestamp": "2026-04-21T11:00:05Z",
                "nodeid": "tests/e2e/test_x.py::test_y",
                "run_id": 88,
            },
            {
                "event": "e2e.test_completed",
                "timestamp": "2026-04-21T11:00:25Z",
                "nodeid": "tests/e2e/test_x.py::test_y",
                "status": "passed",
                "run_id": 88,
            },
            {
                "event": "e2e.run_finished",
                "timestamp": "2026-04-21T11:00:30Z",
                "run_id": 88,
                "status": "passed",
            },
        ],
        agent_events=[
            {
                "event": "session.started",
                "issue_number": 4001,
                "timestamp": "2026-04-21T11:01:00Z",
            },
            {
                "event": "session.completed",
                "issue_number": 4001,
                "timestamp": "2026-04-21T11:02:00Z",
                "status": "completed",
            },
        ],
    )
    payload = container.model_dump(mode="json")

    # Drill into the typed linked-lifecycle cycle for issue 4001.
    runs = payload["runs"]
    linked_lifecycles = runs[0]["e2e_run"]["linked_issue_lifecycles"]
    assert linked_lifecycles, "expected at least one linked-issue lifecycle"
    linked_cycle = linked_lifecycles[0]["cycles"][0]

    # Journey fields are explicitly null (not 0 / "" / False placeholders).
    for journey_field in (
        "lifecycle",
        "iteration",
        "timestamp",
        "agent",
        "reviewer_agent",
        "retry_count",
        "reset_from_scratch",
        "cycle_label",
        "time_label",
        "expanded",
        "artifacts",
        "validation",
    ):
        assert linked_cycle[journey_field] is None, (
            f"E2E linked cycle's {journey_field} must be null when no journey "
            f"context is threaded; got {linked_cycle[journey_field]!r}"
        )
    # Empty journey collections, not absent.
    assert linked_cycle["session_run_ids"] == []
    assert linked_cycle["steps"] == []
    assert linked_cycle["phase_groups"] == []


def test_e2e_run_timeline_payload_matches_ui_openapi() -> None:
    event = _e2e_timeline_event(
        issue_affordances=[{"issue_number": 12, "run_id": 88, "label": "fixture"}],
    )
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [_e2e_timeline_cycle(event)],
        "issue_affordances": [{"issue_number": 12, "run_id": 88}],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    _validator("E2ERunTimelinePayload").validate(payload)
    E2ERunTimelinePayload.model_validate(payload)


def test_e2e_run_timeline_payload_rejects_untyped_aggregate_fields() -> None:
    event = _e2e_timeline_event(unexpected_event_field=True)
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [],
        "issue_affordances": [{"issue_number": 12, "run_id": 88}],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    errors = list(_validator("E2ERunTimelinePayload").iter_errors(payload))

    assert any(
        "unexpected_event_field" in error.message and "Additional properties" in error.message
        for error in errors
    )


def test_e2e_run_timeline_payload_rejects_untyped_cycle_and_affordance_fields() -> None:
    event = _e2e_timeline_event()
    payload = {
        "events": [event],
        "phase_toc": [{"phase": "execution", "label": "Execution"}],
        "cycles": [_e2e_timeline_cycle(event) | {"unexpected_cycle_field": True}],
        "issue_affordances": [
            {"issue_number": 12, "run_id": 88, "unexpected_affordance_field": True},
        ],
        "lifecycle": _e2e_container().model_dump(mode="json"),
    }

    errors = list(_validator("E2ERunTimelinePayload").iter_errors(payload))
    messages = _schema_error_messages(errors)

    assert "unexpected_cycle_field" in messages
    assert "unexpected_affordance_field" in messages


def test_e2e_run_detail_payload_matches_ui_openapi() -> None:
    payload = build_issue_detail_view_model(
        issue_number="e2e-run:88",
        title="E2E Run #88",
        issue_url="",
        events=[_e2e_timeline_event()],
        phase_toc=[{"phase": "execution", "label": "Execution"}],
        cycles=[],
    )
    payload["run"] = {
        "id": 88,
        "orchestrator_id": "test-orch",
        "started_at": "2026-04-21T11:00:00Z",
        "finished_at": "2026-04-21T11:10:00Z",
        "status": "passed",
        "exit_code": 0,
        "duration_seconds": 600.0,
        "pytest_args": ["tests/e2e", "-v"],
        "command": ["pytest", "tests/e2e", "-v"],
        "runner_kind": "pytest",
        "commit_sha": "abc123",
        "branch": "main",
        "log_path": "/tmp/run.log",
        "artifacts_dir": "/tmp/e2e-artifacts/run-88",
        "total_tests": 1,
        "current_test": None,
    }
    payload["results_summary"] = {
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "passed": 1,
        "quarantined": 0,
        "skipped": 0,
        "total": 1,
    }
    payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {
                "nodeid": "tests/e2e/test_example.py::test_passes",
                "case_id": "tests/e2e/test_example.py::test_passes",
                "label": "test_passes",
                "display_name": "test_passes",
                "suite_name": "tests.e2e.test_example",
                "result_source": "junit_xml",
                "outcome": "passed",
                "duration_seconds": 1.2,
                "longrepr": None,
                "failure_summary": None,
                "retry_outcome": None,
                "is_quarantined": False,
                "updated_at": "2026-04-21T11:01:00Z",
                "history": [],
                "existing_issue": None,
                "category": "healthy",
                "result_category": "passed",
                "flip_rate": 0.0,
                "flip_rate_percent": 0.0,
                "is_likely_flaky": False,
            }
        ],
        "quarantined": [],
        "skipped": [],
    }
    payload["artifacts"] = [
        {"kind": "raw_log", "label": "Raw Output", "path": "/tmp/run.log"},
        {"kind": "junit_xml", "label": "JUnit XML", "path": "/tmp/e2e-artifacts/run-88/junit.xml"},
    ]
    payload["reports"] = [
        {"kind": "junit_xml", "label": "JUnit XML", "path": "/tmp/e2e-artifacts/run-88/junit.xml"},
    ]
    payload["issue_affordances"] = [{"issue_number": 12, "run_id": 88}]
    payload["lifecycle"] = _e2e_container().model_dump(mode="json")

    _validator("E2ERunDetailPayload").validate(payload)
    E2ERunDetailPayload.model_validate(payload)


def test_issue_detail_action_payload_accepts_null_optional_url() -> None:
    payload = {"id": "focus", "label": "Focus", "url": None}

    _validator("IssueDetailActionPayload").validate(payload)
    IssueDetailActionPayload.model_validate(payload)


def test_e2e_run_detail_payload_rejects_untyped_detail_fields() -> None:
    payload = build_issue_detail_view_model(
        issue_number="e2e-run:88",
        title="E2E Run #88",
        issue_url="",
        events=[_e2e_timeline_event()],
        phase_toc=[{"phase": "execution", "label": "Execution"}],
        cycles=[],
    )
    payload["run"] = {
        "id": 88,
        "orchestrator_id": "test-orch",
        "started_at": "2026-04-21T11:00:00Z",
        "finished_at": "2026-04-21T11:10:00Z",
        "status": "passed",
        "exit_code": 0,
        "duration_seconds": 600.0,
        "pytest_args": ["tests/e2e", "-v"],
        "command": ["pytest", "tests/e2e", "-v"],
        "runner_kind": "pytest",
        "commit_sha": "abc123",
        "branch": "main",
        "log_path": "/tmp/run.log",
        "artifacts_dir": "/tmp/e2e-artifacts/run-88",
        "total_tests": 1,
        "current_test": None,
    }
    payload["results_summary"] = {
        "untriaged": 0,
        "has_issue": 0,
        "flaky": 0,
        "fixed": 0,
        "passed": 1,
        "quarantined": 0,
        "skipped": 0,
        "total": 1,
    }
    payload["results_by_category"] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [],
        "quarantined": [],
        "skipped": [],
    }
    payload["artifacts"] = []
    payload["reports"] = []
    payload["issue_affordances"] = [{"issue_number": 12, "run_id": 88}]
    payload["lifecycle"] = _e2e_container().model_dump(mode="json")
    payload["summary"]["unexpected_summary_field"] = True
    payload["actions"][0]["unexpected_action_field"] = True
    payload["blocked_detail"] = {
        "reason": "Blocked",
        "labels": ["blocked"],
        "rework_info": None,
        "event_summary": "waiting",
        "unexpected_blocked_detail_field": True,
    }

    errors = list(_validator("E2ERunDetailPayload").iter_errors(payload))
    messages = _schema_error_messages(errors)

    assert "unexpected_summary_field" in messages
    assert "unexpected_action_field" in messages
    assert "unexpected_blocked_detail_field" in messages


def test_lifecycle_dashboard_container_payload_matches_ui_openapi() -> None:
    container = DashboardTimelineContainer(
        subject=TimelineSubject(kind="dashboard", id="dashboard", label="Dashboard"),
        current=DashboardIteration(
            subject=TimelineSubject(
                kind="dashboard",
                id="current",
                label="Current Dashboard",
            ),
            issue_lifecycles=(_issue_lifecycle(12),),
        ),
    )

    _validator("LifecycleTimelineContainerPayload").validate(
        container.model_dump(mode="json")
    )


def test_lifecycle_e2e_container_payload_matches_ui_openapi() -> None:
    _validator("LifecycleTimelineContainerPayload").validate(
        _e2e_container().model_dump(mode="json")
    )


def _e2e_container() -> E2ESuiteTimelineContainer:
    run = E2ERunLifecycle(
        run_id=88,
        started_at="2026-04-21T11:00:00Z",
        completed_at="2026-04-21T11:02:00Z",
        tests=(
            PassedE2ETestExecution(
                nodeid="tests/e2e/test_example.py::test_passes",
                started_at="2026-04-21T11:00:00Z",
                completed_at="2026-04-21T11:01:00Z",
                commands=(ShowEventDetailsCommand(event_ref="event:e2e-test"),),
            ),
        ),
        linked_issue_lifecycles=(_issue_lifecycle(12),),
    )
    return E2ESuiteTimelineContainer(
        subject=TimelineSubject(kind="e2e_suite", id="suite", label="E2E Suite"),
        runs=(
            E2ERunIteration(
                subject=TimelineSubject(kind="e2e_run", id="88", label="Run #88"),
                e2e_run=run,
            ),
        ),
    )


def _issue_lifecycle(issue_number: int) -> IssueLifecycle:
    return IssueLifecycle(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        cycles=(
            IssueCycle(
                cycle_number=1,
                coder=CompletedCodingAttempt(
                    issue_number=issue_number,
                    agent=AgentIdentity(name="codex", role="coder"),
                    started_at="2026-04-21T10:00:00Z",
                    completed_at="2026-04-21T10:10:00Z",
                    completion_record=CompletionRecordEvidence(
                        path=f"/runs/issue-{issue_number}/completion-record.json",
                    ),
                    validation=ValidationPassed(
                        command="pytest tests/unit -q",
                        record_path=f"/runs/issue-{issue_number}/validation.json",
                        details_command=OpenValidationDetailsCommand(
                            issue_number=issue_number,
                            run_dir=f"/runs/issue-{issue_number}",
                        ),
                    ),
                    session_recording=SessionRecordingUnavailable(
                        reason="fixture has no recording",
                    ),
                    commands=(
                        ShowEventDetailsCommand(event_ref=f"event:issue:{issue_number}"),
                        OpenCompletionRecordCommand(
                            path=f"/runs/issue-{issue_number}/completion-record.json",
                        ),
                    ),
                ),
                review=ReviewNotReached(reason="not_required"),
                outcome=_ob("Completed"),
            ),
        ),
    )
