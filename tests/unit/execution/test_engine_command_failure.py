"""Classification of failed Control Center -> engine commands (#8222)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from issue_orchestrator.execution.engine_command_failure import (
    UPSTREAM_BODY_EXCERPT_CHARS,
    EngineCommandFailure,
    EngineCommandFailureKind,
    describe_engine_command_failure,
    non_object_body_failure,
)

URL = "http://127.0.0.1:8081"


def _describe(exc: httpx.HTTPError | json.JSONDecodeError) -> EngineCommandFailure:
    return describe_engine_command_failure(exc, command="pause", url=URL, timeout_seconds=120)


@pytest.mark.parametrize(
    ("exc", "kind", "status"),
    [
        (httpx.ReadTimeout(""), EngineCommandFailureKind.NO_ANSWER, 504),
        (httpx.WriteTimeout(""), EngineCommandFailureKind.NO_ANSWER, 504),
        (httpx.ConnectTimeout(""), EngineCommandFailureKind.UNREACHABLE, 502),
        (httpx.ConnectError(""), EngineCommandFailureKind.UNREACHABLE, 502),
        (httpx.RemoteProtocolError(""), EngineCommandFailureKind.UNREACHABLE, 502),
    ],
)
def test_messageless_transport_errors_still_get_a_cause(
    exc: httpx.HTTPError, kind: EngineCommandFailureKind, status: int
) -> None:
    """httpx raises these with an empty message — the original empty detail."""
    failure = _describe(exc)

    assert failure.kind is kind
    assert failure.http_status == status
    assert type(exc).__name__ in failure.detail
    assert URL in failure.detail and "pause" in failure.detail


def test_upstream_status_carries_status_and_bounded_body() -> None:
    body = "x" * (UPSTREAM_BODY_EXCERPT_CHARS + 50)
    request = httpx.Request("POST", f"{URL}/api/pause")
    response = httpx.Response(401, text=body, request=request)
    exc = httpx.HTTPStatusError("401", request=request, response=response)

    failure = _describe(exc)

    assert failure.kind is EngineCommandFailureKind.UPSTREAM_ERROR
    assert failure.upstream_status == 401
    assert failure.upstream_body is not None
    assert failure.upstream_body.endswith("…[truncated]")
    assert len(failure.upstream_body) < len(body)
    assert "HTTP 401" in failure.detail
    payload = failure.to_payload()
    assert payload["error"] == "passthrough_failed"
    assert payload["failure"] == "upstream_error"
    assert payload["upstream_status"] == 401


def test_a_non_json_body_is_quoted() -> None:
    try:
        json.loads("<html>gateway</html>")
    except json.JSONDecodeError as exc:
        failure = _describe(exc)
    assert failure.kind is EngineCommandFailureKind.INVALID_BODY
    assert "<html>gateway</html>" in failure.detail


def test_a_json_answer_that_is_not_an_object_is_invalid() -> None:
    failure = non_object_body_failure([1, 2], command="pause", url=URL, upstream_status=200)
    assert failure.kind is EngineCommandFailureKind.INVALID_BODY
    assert "[1, 2]" in failure.detail and "HTTP 200" in failure.detail


def test_a_failure_cannot_be_built_without_a_cause() -> None:
    with pytest.raises(ValueError, match="detail"):
        EngineCommandFailure(
            kind=EngineCommandFailureKind.NO_ANSWER, command="pause", url=URL, detail="  "
        )


def test_a_control_center_bug_is_not_dressed_up_as_an_engine_failure() -> None:
    bug: Any = KeyError("x")
    with pytest.raises(TypeError, match="KeyError"):
        describe_engine_command_failure(
            bug,
            command="pause",
            url=URL,
            timeout_seconds=1,
        )
