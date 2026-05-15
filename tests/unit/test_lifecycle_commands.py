"""Snapshot tests for ``TimelineCommand`` typed models (issue #6310 PR 3).

One fixture per ``TimelineCommand`` kind, asserted via
``.model_dump(mode="json")``.  These pin the wire shape every Command
discriminator produces — adding a new kind requires adding a fixture, and
renaming a field on an existing kind fails an exact-shape assertion before
the JS dispatch table notices.

Tests intentionally do not call the model_dump_json round-trip helpers
because the goal here is to lock the *wire shape* of each Command kind
that ``runLifecycleCommand`` consumes on the frontend, not the
generic Pydantic serializer.
"""

from __future__ import annotations

from typing import Any

import pytest

from issue_orchestrator.view_models.lifecycle_semantics import (
    CycleValidationBadge,
    OpenCompletionRecordCommand,
    OpenE2ERunCommand,
    OpenInlineAgentAttemptsCommand,
    OpenIssueTimelineCommand,
    OpenReviewFeedbackCommand,
    OpenSessionRecordingCommand,
    OpenValidationDetailsCommand,
    ShowEventDetailsCommand,
)


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def test_show_event_details_command_wire_shape() -> None:
    assert _dump(ShowEventDetailsCommand(event_ref="evt-123")) == {
        "kind": "show_event_details",
        "label": "Event Details",
        "event_ref": "evt-123",
    }


def test_open_session_recording_command_wire_shape() -> None:
    assert _dump(
        OpenSessionRecordingCommand(
            issue_number=4124,
            run_dir="/tmp/run-1",
            session_role="coder",
            round_index=2,
        )
    ) == {
        "kind": "open_session_recording",
        "label": "Session Recording",
        "issue_number": 4124,
        "run_dir": "/tmp/run-1",
        "session_role": "coder",
        "round_index": 2,
    }


def test_open_validation_details_command_wire_shape() -> None:
    assert _dump(
        OpenValidationDetailsCommand(issue_number=4124, run_dir="/tmp/run-1")
    ) == {
        "kind": "open_validation_details",
        "label": "Validation Details",
        "issue_number": 4124,
        "run_dir": "/tmp/run-1",
    }


def test_open_completion_record_command_wire_shape() -> None:
    assert _dump(OpenCompletionRecordCommand(path="/tmp/completion.json")) == {
        "kind": "open_completion_record",
        "label": "Completion Record",
        "path": "/tmp/completion.json",
    }


def test_open_review_feedback_command_wire_shape() -> None:
    assert _dump(
        OpenReviewFeedbackCommand(issue_number=4124, event_ref="review-evt-9")
    ) == {
        "kind": "open_review_feedback",
        "label": "Review Feedback",
        "issue_number": 4124,
        "event_ref": "review-evt-9",
    }


def test_open_issue_timeline_command_wire_shape_dashboard_scope() -> None:
    assert _dump(
        OpenIssueTimelineCommand(issue_number=4124, scope_kind="dashboard")
    ) == {
        "kind": "open_issue_timeline",
        "label": "Issue Timeline",
        "issue_number": 4124,
        "scope_kind": "dashboard",
        "e2e_run_id": None,
    }


def test_open_issue_timeline_command_wire_shape_e2e_scope() -> None:
    assert _dump(
        OpenIssueTimelineCommand(
            issue_number=4124,
            scope_kind="e2e_run",
            e2e_run_id=88,
        )
    ) == {
        "kind": "open_issue_timeline",
        "label": "Issue Timeline",
        "issue_number": 4124,
        "scope_kind": "e2e_run",
        "e2e_run_id": 88,
    }


def test_open_issue_timeline_command_e2e_scope_requires_run_id() -> None:
    with pytest.raises(ValueError, match="e2e_run_id"):
        OpenIssueTimelineCommand(
            issue_number=4124, scope_kind="e2e_run", e2e_run_id=None
        )


def test_open_e2e_run_command_wire_shape_default_expand() -> None:
    """``OpenE2ERunCommand`` (issue #6322): default ``expand_run_details=False``."""
    assert _dump(OpenE2ERunCommand(run_id=88)) == {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 88,
        "expand_run_details": False,
    }


def test_open_e2e_run_command_wire_shape_expand_true() -> None:
    """The ``expand_run_details=True`` variant carries through to the dispatch."""
    assert _dump(OpenE2ERunCommand(run_id=88, expand_run_details=True)) == {
        "kind": "open_e2e_run",
        "label": "Open E2E Run",
        "run_id": 88,
        "expand_run_details": True,
    }


def test_open_e2e_run_command_rejects_non_positive_run_id() -> None:
    """``run_id`` must be a positive integer.

    The frontend dispatcher already guards on truthy ``run_id``, but
    the Pydantic validator is the contract-level source of truth.
    Zero and negative values must fail validation, not silently pass
    through to the JS guard.
    """
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id=0)
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id=-1)


def test_open_e2e_run_command_rejects_string_run_id_strict() -> None:
    """``run_id`` is strict-int — no coercion from strings.

    PR #6329 round-5 blocker: Pydantic's default ``int`` type coerces
    ``"88"`` → 88, while JSON Schema's ``type: integer`` rejects it.
    The canonical command model uses ``Field(strict=True)`` to match
    the wire-contract semantics — malformed strings must fail, not
    silently normalize.
    """
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id="88")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id="1")  # type: ignore[arg-type]


def test_open_e2e_run_command_rejects_boolean_run_id_strict() -> None:
    """``run_id`` is strict-int — no coercion from booleans either.

    Python's ``bool`` is a subclass of ``int``, so without
    ``strict=True`` Pydantic accepts ``True`` (becomes 1) and
    ``False`` (becomes 0).  Both are malformed wire payloads that
    JSON Schema rejects; the strict-int field rejects them too.
    """
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OpenE2ERunCommand(run_id=False)  # type: ignore[arg-type]


def test_open_inline_agent_attempts_command_wire_shape() -> None:
    """``OpenInlineAgentAttemptsCommand`` (issue #6322 follow-up):
    the typed Command that the inline ``▸ Attempts on issue #N``
    expander emits on its ``<details>`` element.  Backs the
    ``open_inline_agent_attempts`` branch in the JS dispatcher.
    """
    assert _dump(OpenInlineAgentAttemptsCommand(issue_number=4503)) == {
        "kind": "open_inline_agent_attempts",
        "label": "Open Inline Agent Attempts",
        "issue_number": 4503,
    }


def test_open_inline_agent_attempts_command_rejects_non_positive_issue_number() -> None:
    """``issue_number`` must be a positive integer.

    GitHub issue numbers are always >= 1; zero or negative payloads
    are malformed by definition.
    """
    with pytest.raises(ValueError):
        OpenInlineAgentAttemptsCommand(issue_number=0)
    with pytest.raises(ValueError):
        OpenInlineAgentAttemptsCommand(issue_number=-1)


def test_open_inline_agent_attempts_command_rejects_string_issue_number_strict() -> None:
    """``issue_number`` is strict-int — no coercion from strings.

    Same invariant as ``OpenE2ERunCommand.run_id`` (PR #6329
    round-5): Pydantic's default ``int`` coerces ``"4503"`` to
    ``4503``, while JSON Schema rejects it.  The canonical model
    matches the wire contract via ``Field(strict=True)``.
    """
    with pytest.raises(ValueError):
        OpenInlineAgentAttemptsCommand(issue_number="4503")  # type: ignore[arg-type]


def test_open_inline_agent_attempts_command_rejects_boolean_issue_number_strict() -> None:
    """``issue_number`` is strict-int — no coercion from booleans.

    ``bool`` is a ``int`` subclass; without ``strict=True``
    ``True`` becomes 1 and ``False`` becomes 0.  Both are
    malformed payloads.
    """
    with pytest.raises(ValueError):
        OpenInlineAgentAttemptsCommand(issue_number=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OpenInlineAgentAttemptsCommand(issue_number=False)  # type: ignore[arg-type]


def test_cycle_validation_badge_wire_shape_for_each_state() -> None:
    """``CycleValidationBadge`` (issue #6310 AC-2) has four canonical
    states; this pins the wire shape per state.  ``passed`` / ``failed``
    carry an ``OpenValidationDetailsCommand``; ``pending`` /
    ``not_validated`` have no command."""
    command = OpenValidationDetailsCommand(issue_number=4124, run_dir="/tmp/r")

    assert _dump(CycleValidationBadge(state="pending")) == {
        "state": "pending",
        "command": None,
    }
    assert _dump(CycleValidationBadge(state="not_validated")) == {
        "state": "not_validated",
        "command": None,
    }
    assert _dump(CycleValidationBadge(state="passed", command=command)) == {
        "state": "passed",
        "command": {
            "kind": "open_validation_details",
            "label": "Validation Details",
            "issue_number": 4124,
            "run_dir": "/tmp/r",
        },
    }
    assert _dump(CycleValidationBadge(state="failed", command=command)) == {
        "state": "failed",
        "command": {
            "kind": "open_validation_details",
            "label": "Validation Details",
            "issue_number": 4124,
            "run_dir": "/tmp/r",
        },
    }


@pytest.mark.parametrize(
    "state",
    ["passed", "failed"],
)
def test_cycle_validation_badge_rejects_missing_command_for_terminal_states(
    state: str,
) -> None:
    with pytest.raises(ValueError, match="command required"):
        CycleValidationBadge(state=state, command=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    ["pending", "not_validated"],
)
def test_cycle_validation_badge_rejects_command_for_non_terminal_states(
    state: str,
) -> None:
    command = OpenValidationDetailsCommand(issue_number=1, run_dir="/tmp/r")
    with pytest.raises(ValueError, match="command must be absent"):
        CycleValidationBadge(state=state, command=command)  # type: ignore[arg-type]
