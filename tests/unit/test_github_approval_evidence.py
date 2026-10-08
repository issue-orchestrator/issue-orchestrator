"""The GitHub side of approval evidence (#7763): label events and roles."""

from __future__ import annotations

from types import SimpleNamespace
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


def test_the_standing_run_spans_pages_oldest_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json=[_labeled(1, "Approved", "lead")]
                + [_labeled(2, "other", "lead") for _ in range(99)],
            )
        return httpx.Response(200, json=[_labeled(3, "approved", "io-bot[bot]")])

    client = _client_with_transport(httpx.MockTransport(handler))

    run = client.standing_label_events(5, "approved")

    assert [event["id"] for event in run] == [1, 3]


def test_only_a_standing_transition_is_returned() -> None:
    """#7763 review r8 F1: a later opposite transition voids an earlier one."""
    events = [_labeled(1, "approved", "lead"), _labeled(2, "approved", "x", kind="unlabeled")]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=events)))

    assert client.standing_label_events(5, "approved") == []
    assert [e["id"] for e in client.standing_label_events(5, "approved", removed=True)] == [2]

    readded = [*events, _labeled(3, "approved", "lead")]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=readded)))

    assert [e["id"] for e in client.standing_label_events(5, "approved")] == [3]
    assert client.standing_label_events(5, "approved", removed=True) == []


def test_an_approval_removed_after_the_issue_read_never_consents() -> None:
    """#7763 review r8 F1: the issue snapshot still shows `approved`, but the
    maintainer removed it before the event read. Neither the launch boundary
    nor the op-execution consent may act on the old `labeled` event."""
    from issue_orchestrator.control.tech_lead_approval import (
        TechLeadApprovals,
        unapproved_proposal_launch,
    )
    from issue_orchestrator.domain.models import Issue
    from issue_orchestrator.domain.tech_lead_approval import with_proposal_marker
    from issue_orchestrator.ports.approval_evidence import (
        InMemoryOperatorApprovalRecords,
        InMemoryProposalIssueIndex,
    )

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[
                _labeled(1, "approved", "lead"),
                _labeled(2, "approved", "lead", kind="unlabeled"),  # after the issue read
            ])
        return httpx.Response(200, json={"permission": "admin", "role_name": "admin"})

    client = _client_with_transport(httpx.MockTransport(github))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())
    approvals = TechLeadApprovals(adapter, InMemoryOperatorApprovalRecords(), InMemoryProposalIssueIndex(), lambda: ())
    snapshot = Issue(number=5, title="t", labels=["tech-lead-proposal", "approved"], state="open",
                     repo="owner/repo", body=with_proposal_marker("b"))
    repository = MagicMock()
    repository.get_issue.return_value = snapshot

    assert unapproved_proposal_launch(5, repository, approvals) is not None
    assert not approvals.confirm(snapshot)


def test_a_reopen_voids_every_earlier_approval() -> None:
    """#7763 review r7 F2: closing declines; an approval from before the
    decline never carries over to the reopened proposal."""
    events = [
        _labeled(1, "approved", "lead"),
        {"id": 2, "event": "closed", "actor": {"login": "lead", "type": "User"}},
        {"id": 3, "event": "reopened", "actor": {"login": "lead", "type": "User"}},
    ]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=events)))

    assert client.standing_label_events(5, "approved") == []

    renewed = [*events, _labeled(4, "approved", "lead")]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=renewed)))

    assert [e["id"] for e in client.standing_label_events(5, "approved")] == [4]


def test_a_block_label_kept_on_through_a_close_and_reopen_is_the_same_application() -> None:
    """#8688 review r3 F1: a close voids an approval, never a block's label.
    The block-episode reader's run ends only on the label's own removal."""
    events = [
        _labeled(1, "needs-human", "io-bot[bot]"),
        {"id": 2, "event": "closed", "actor": {"login": "lead", "type": "User"}},
        {"id": 3, "event": "reopened", "actor": {"login": "lead", "type": "User"}},
    ]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=events)))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())

    assert client.standing_label_events(5, "needs-human") == []  # approval semantics
    application = adapter.label_application(5, "needs-human")
    assert application is not None and application.event_id == 1

    removed = [*events, _labeled(4, "needs-human", "lead", kind="unlabeled")]
    client = _client_with_transport(httpx.MockTransport(lambda request: httpx.Response(200, json=removed)))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())
    assert adapter.label_application(5, "needs-human") is None


def test_one_events_read_dates_every_blocking_label_of_an_item() -> None:
    """#8731: every blocking label of an item is dated from ONE scan of its
    events, each by the first event of its own standing run: a label removed
    and re-applied is a new application, one never applied is not standing,
    and a close never ends a block label's run."""
    events = [
        _labeled(1, "Blocked-Failed", "io-bot[bot]"),
        _labeled(2, "publish-failed", "io-bot[bot]"),
        {"id": 3, "event": "closed", "actor": {"login": "lead", "type": "User"}},
        {"id": 4, "event": "reopened", "actor": {"login": "lead", "type": "User"}},
        _labeled(5, "publish-failed", "io-bot[bot]", kind="unlabeled"),
        _labeled(6, "publish-failed", "io-bot[bot]"),
        _labeled(7, "blocked-failed", "lead"),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=events)

    client = _client_with_transport(httpx.MockTransport(handler))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())

    applications = adapter.label_applications(5, ("blocked-failed", "publish-failed", "recovery-pending"))

    assert {label: event and event.event_id for label, event in applications.items()} == {
        "blocked-failed": 1, "publish-failed": 6, "recovery-pending": None,
    }
    assert len(requests) == 1


def test_no_matching_event_is_none_only_after_the_final_page() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda request: httpx.Response(200, json=[_labeled(1, "bug", "lead")]))
    )

    assert client.standing_label_events(5, "approved") == []


def test_a_malformed_event_page_fails_loud() -> None:
    client = _client_with_transport(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"message": "nope"}))
    )

    with pytest.raises(GitHubScanIncompleteError):
        client.standing_label_events(5, "approved")


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
    client.standing_label_events.return_value = [payload] if payload is not None else []
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
    standing = _adapter(payload).standing_label(5, "approved")

    assert standing is not None and standing.application.actor_is_bot


def test_the_adapter_reads_a_person() -> None:
    standing = _adapter(_labeled(9, "approved", "lead")).standing_label(5, "approved")

    assert standing is not None
    event = standing.application
    assert (event.event_id, event.actor_login, event.actor_is_bot) == (9, "lead", False)


def test_an_event_without_an_actor_fails_loud() -> None:
    payload = _labeled(9, "approved", "lead")
    del payload["actor"]

    with pytest.raises(GitHubHttpError, match="no actor"):
        _adapter(payload).standing_label(5, "approved")


@pytest.mark.parametrize(
    "intervening",
    [
        {"id": 2, "event": "closed", "actor": {"login": "lead", "type": "User"}},
        {"id": 2, "event": "unlabeled", "actor": {"login": "lead", "type": "User"}},  # r23 F2: no label
        {"id": 2, "actor": {"login": "lead", "type": "User"}},  # r26 F2: no event kind
        "not an event",  # r26 F2: not an object
    ],
    ids=["closed", "unattributable-removal", "no-event-kind", "not-an-object"],
)
def test_an_approval_closed_after_the_issue_read_never_executes_its_op(intervening) -> None:
    """#7763 review r17 F1: the snapshot is open and approved, but the issue
    was closed (declined) before the event read; the op never executes."""
    from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
    from issue_orchestrator.control.tech_lead_proposal_execution import execute_approved_tech_lead_op
    from issue_orchestrator.domain.models import Issue
    from issue_orchestrator.domain.tech_lead_approval import with_proposal_marker
    from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
    from issue_orchestrator.ports.approval_evidence import (
        InMemoryOperatorApprovalRecords,
        InMemoryProposalIssueIndex,
    )
    from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[_labeled(1, "approved", "lead"), intervening])
        return httpx.Response(200, json={"permission": "admin", "role_name": "admin"})

    client = _client_with_transport(httpx.MockTransport(github))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())
    approvals = TechLeadApprovals(adapter, InMemoryOperatorApprovalRecords(), InMemoryProposalIssueIndex(), lambda: ())
    snapshot = Issue(number=5, title="t", labels=["tech-lead-proposal", "awaiting-approval", "approved"],
                     state="open", repo="owner/repo", body=with_proposal_marker("b"))
    host = MagicMock()
    host.get_issue.return_value = snapshot
    ops = InMemoryTechLeadAuthorityStore()
    ops.record_op(issue_number=5, op=StoredTechLeadOp(
        op_type="reset_retry", target_issue_number=13, rationale="r", source_run_id="run",
        source_session_name="s", source_action_id="A1", created_at="2026-10-03T00:00:00Z"))
    apply_fn = MagicMock()

    result = execute_approved_tech_lead_op(
        SimpleNamespace(proposal_issue_number=5), apply_fn,
        repository_host=host, ops=ops, approvals=approvals,
    )

    assert not result.success
    apply_fn.assert_not_called()
    assert ops.load_op(issue_number=5) is not None


#: Issue #8268's real event history from tech-lead exam case H at a0b61b2
#: (#8346), oldest first: the harness filed the proposal with its maintainer
#: token, its GitHub App added `approved` two seconds later, and only THEN did
#: GitHub emit the filing's labeled events — attributing every label the
#: issue carried at that moment, the bot's `approved` included, to the
#: issue's author.
ISSUE_8268_EVENTS = [
    _labeled(32709419257, "approved", "issue-orchestrator-bot[bot]"),
    _labeled(32709427381, "io-e2e-test-data", "BruceBGordon"),
    _labeled(32709427951, "agent:exam-coder", "BruceBGordon"),
    _labeled(32709428447, "tech-lead-proposal", "BruceBGordon"),
    _labeled(32709428936, "awaiting-approval", "BruceBGordon"),
    _labeled(32709429523, "approved", "BruceBGordon"),
    _labeled(32709430322, "io:e2e:exam-h-2c5b138383f9", "BruceBGordon"),
]


def _approvals_over(events: list) -> tuple:
    from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
    from issue_orchestrator.ports.approval_evidence import (
        InMemoryOperatorApprovalRecords,
        InMemoryProposalIssueIndex,
    )

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json={"permission": "admin", "role_name": "admin"})

    client = _client_with_transport(httpx.MockTransport(github))
    adapter = GitHubAdapter(repo="owner/repo", http_client=client, cache=MagicMock(), verification_service=MagicMock())
    return adapter, TechLeadApprovals(
        adapter, InMemoryOperatorApprovalRecords(), InMemoryProposalIssueIndex(), lambda: ()
    )


def test_a_bot_approval_the_filing_attributed_to_its_maintainer_author_never_approves() -> None:
    """#8346: the NEWEST `approved` event names a maintainer, but the label
    stands because a bot applied it; neither admission, launch nor op
    execution may count it."""
    from issue_orchestrator.control.tech_lead_approval import unapproved_proposal_launch
    from issue_orchestrator.domain.models import Issue
    from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdictKind, with_proposal_marker

    adapter, approvals = _approvals_over(ISSUE_8268_EVENTS)
    snapshot = Issue(number=8268, title="t", labels=["tech-lead-proposal", "awaiting-approval", "approved"],
                     state="open", repo="owner/repo", body=with_proposal_marker("b"))
    repository = MagicMock()
    repository.get_issue.return_value = snapshot

    standing = adapter.standing_label(8268, "approved")
    assert standing is not None
    assert [e.actor_login for e in standing.events] == ["issue-orchestrator-bot[bot]", "BruceBGordon"]
    verdict = approvals.verify(snapshot)
    assert verdict.kind is ApprovalVerdictKind.BOT_ACTOR
    assert verdict.actor == "issue-orchestrator-bot[bot]"
    assert not approvals.confirm(snapshot)
    assert unapproved_proposal_launch(8268, repository, approvals) is not None


def test_a_maintainer_approval_with_no_other_application_still_approves() -> None:
    """The fix refuses only what it must: the maintainer approval of the same
    run (case H's #8266) verifies."""
    from issue_orchestrator.domain.models import Issue
    from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdictKind, with_proposal_marker

    events = [
        _labeled(32709390036, "tech-lead-proposal", "BruceBGordon"),
        _labeled(32709392214, "awaiting-approval", "BruceBGordon"),
        _labeled(32709413365, "approved", "BruceBGordon"),
    ]
    _, approvals = _approvals_over(events)
    snapshot = Issue(number=8266, title="t", labels=["tech-lead-proposal", "awaiting-approval", "approved"],
                     state="open", repo="owner/repo", body=with_proposal_marker("b"))

    assert approvals.verify(snapshot).kind is ApprovalVerdictKind.MAINTAINER
