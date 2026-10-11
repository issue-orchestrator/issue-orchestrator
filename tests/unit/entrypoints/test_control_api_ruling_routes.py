"""The maintainer's ruling command on the Control API (#8141)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.domain.standing_ruling import RulingAuthority, parse_rulings_block
from issue_orchestrator.entrypoints._auth_middleware import is_agent_callback_route
from issue_orchestrator.entrypoints.control_api import control_app, set_orchestrator
from tests.standing_ruling_helpers import IssueBodies, rulings_owner

SPEC = "## Outcome\n\nThe spec."


@pytest.fixture
def engine():
    bodies = IssueBodies({364: SPEC})
    orchestrator = MagicMock()
    orchestrator.deps.standing_rulings = rulings_owner(bodies)
    set_orchestrator(orchestrator)
    try:
        yield TestClient(control_app), bodies
    finally:
        set_orchestrator(None)


def test_a_maintainer_ruling_is_recorded_in_the_issue_body(engine) -> None:
    client, bodies = engine

    response = client.post("/api/issues/364/rulings", json={
        "text": "Runtime stamping replaces the static symbol-walk checker.",
        "files": ["tools/walk"], "claims": ["the walk checker is retired"],
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["outcome"] == "recorded" and payload["ruling_id"].startswith("m-")
    [ruling] = parse_rulings_block(bodies.bodies[364])
    assert ruling.ruling_id == payload["ruling_id"]
    assert ruling.authority is RulingAuthority.MAINTAINER
    assert ruling.scope.files == ("tools/walk",) and ruling.scope.claims == ("the walk checker is retired",)
    listed = client.get("/api/issues/364/rulings").json()["rulings"]
    assert [item["id"] for item in listed] == [ruling.ruling_id]


@pytest.mark.parametrize("body", [
    {"files": ["x"]},
    {"text": "  "},
    {"text": "ok", "files": ["/etc/passwd"]},
    {"text": "ok", "authority": "approved_decision"},
    ["not", "an", "object"],
])
def test_an_invalid_request_is_refused_and_writes_nothing(engine, body) -> None:
    client, bodies = engine

    response = client.post("/api/issues/364/rulings", json=body)

    assert response.status_code == 400 and bodies.writes == []


def test_unreadable_rulings_are_a_conflict_not_a_write(engine) -> None:
    client, bodies = engine
    bodies.unreadable.add(364)

    response = client.post("/api/issues/364/rulings", json={"text": "A ruling."})

    assert response.status_code == 409 and bodies.writes == []


def test_a_ruling_is_retired_by_id(engine) -> None:
    client, bodies = engine
    ruling_id = client.post("/api/issues/364/rulings", json={"text": "A ruling to retire."}).json()["ruling_id"]

    assert client.delete(f"/api/issues/364/rulings/{ruling_id}").status_code == 200
    assert bodies.bodies[364] == SPEC
    assert client.delete(f"/api/issues/364/rulings/{ruling_id}").status_code == 404


def test_an_agent_token_can_never_record_a_ruling() -> None:
    assert not is_agent_callback_route("/api/issues/364/rulings")
    assert not is_agent_callback_route("/api/issues/364/rulings/m-0123456789ab")


def test_no_engine_is_unavailable() -> None:
    set_orchestrator(None)
    assert TestClient(control_app).post("/api/issues/1/rulings", json={"text": "x"}).status_code == 503


def test_a_retried_request_records_the_ruling_once(engine) -> None:
    """The body write landed but the response was lost: the retry must not add a second ruling."""
    client, bodies = engine
    request = {"text": "Runtime stamping replaces the walk checker.", "files": ["tools/walk"]}

    first = client.post("/api/issues/364/rulings", json=request).json()
    again = client.post("/api/issues/364/rulings", json=request).json()

    assert first["ruling_id"] == again["ruling_id"] and again["outcome"] == "already_recorded"
    assert len(parse_rulings_block(bodies.bodies[364])) == 1
    other = client.post("/api/issues/364/rulings", json={**request, "text": "A different ruling."}).json()
    assert other["ruling_id"] != first["ruling_id"] and len(parse_rulings_block(bodies.bodies[364])) == 2
