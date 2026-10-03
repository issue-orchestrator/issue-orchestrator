"""The GitHub side of approval evidence (#7763): label events and roles."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError, GitHubScanIncompleteError
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from tests.unit.test_github_http import _client_with_transport


def _labeled(event_id: int, label: str, login: str, *, kind: str = "labeled", **extra) -> dict:
    return {
        "id": event_id,
        "event": kind,
        "label": {"name": label},
        "actor": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
        "created_at": f"2026-10-03T00:00:{event_id:02d}Z",
        **extra,
    }


def test_the_latest_matching_labeled_event_wins_across_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json=[_labeled(1, "Approved", "lead")]
                + [_labeled(2, "other", "lead") for _ in range(99)],
            )
        return httpx.Response(200, json=[_labeled(3, "approved", "io-bot[bot]"), _labeled(4, "approved", "x", kind="unlabeled")])

    client = _client_with_transport(httpx.MockTransport(handler))

    event = client.latest_label_event(5, "approved")

    assert event is not None and event["id"] == 3
    assert client.latest_label_event(5, "approved", removed=True)["id"] == 4


def test_a_reopen_voids_every_earlier_approval() -> None:
    """#7763 review r7 F2: closing declines; an approval from before the
    decline never carries over to the reopened proposal."""
    events = [
        _labeled(1, "approved", "lead"),
        {"id": 2, "event": "closed", "actor": {"login": "lead", "type": "User"}},
        {"id": 3, "event": "reopened", "actor": {"login": "lead", "type": "User"}},
    ]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=events)))

    assert client.latest_label_event(5, "approved") is None

    renewed = [*events, _labeled(4, "approved", "lead")]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=renewed)))

    assert client.latest_label_event(5, "approved")["id"] == 4


def test_no_matching_event_is_none_only_after_the_final_page() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda request: httpx.Response(200, json=[_labeled(1, "bug", "lead")]))
    )

    assert client.latest_label_event(5, "approved") is None


def test_a_malformed_event_page_fails_loud() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"message": "nope"}))
    )

    with pytest.raises(GitHubScanIncompleteError):
        client.latest_label_event(5, "approved")


def test_repository_role_prefers_the_fine_grained_role() -> None:
    client = _client_with_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"permission": "write", "role_name": "maintain"})
        )
    )

    assert client.repository_role("lead") == "maintain"


def test_an_unknown_user_has_no_role() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda request: httpx.Response(404, json={"message": "Not Found"}))
    )

    assert client.repository_role("ghost") is None


def _adapter(payload: dict | None) -> GitHubAdapter:
    client = MagicMock()
    client.latest_label_event.return_value = payload
    return GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())


@pytest.mark.parametrize(
    "payload",
    [
        _labeled(9, "approved", "io-bot[bot]"),
        {**_labeled(9, "approved", "lead"), "actor": {"login": "lead", "type": "Bot"}},
        {**_labeled(9, "approved", "lead"), "performed_via_github_app": {"slug": "io"}},
    ],
    ids=["bot-login", "bot-type", "via-app"],
)
def test_the_adapter_marks_every_kind_of_automation(payload) -> None:
    event = _adapter(payload).latest_label_event(5, "approved")

    assert event is not None and event.actor_is_bot


def test_the_adapter_reads_a_person() -> None:
    event = _adapter(_labeled(9, "approved", "lead")).latest_label_event(5, "approved")

    assert event is not None
    assert (event.event_id, event.actor_login, event.actor_is_bot) == (9, "lead", False)


def test_an_event_without_an_actor_fails_loud() -> None:
    payload = _labeled(9, "approved", "lead")
    del payload["actor"]

    with pytest.raises(GitHubHttpError, match="no actor"):
        _adapter(payload).latest_label_event(5, "approved")
