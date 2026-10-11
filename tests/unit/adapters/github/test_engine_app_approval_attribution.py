"""The Control Center's Approve binds on the engine's own GitHub identity (#8987).

Porchpin, 2026-10-08: every Control Center Approve on the App-authenticated
engine was refused ("cannot be attributed to this engine's write"), and the
next tick stripped the label as "applied by automation (@porchpin-bot[bot])".
GitHub records an App installation's label write as its BOT ACCOUNT
(``<slug>[bot]``, type ``Bot``) and leaves the event's
``performed_via_github_app`` null; the engine looked for its App there.

These tests drive the real command, approval owner, adapter, HTTP client and
App auth against a fake GitHub whose issue events have exactly the shape
GitHub returned for issue-orchestrator#8268's App-written ``approved``
(event 32709419257): actor ``issue-orchestrator-bot[bot]``, ``type: Bot``,
account id 301493143, ``performed_via_github_app: null``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx
import pytest

from issue_orchestrator.adapters.github.auth import GitHubAppInstallationTokenProvider, GitHubAuth
from issue_orchestrator.adapters.github.errors import GitHubAuthError
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.adapters.github.tokens import GitHubAppAuthConfig
from issue_orchestrator.control.tech_lead_actions import SettleProposalApprovalAction
from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
from issue_orchestrator.control.tech_lead_approval_writes import (
    apply_operator_proposal_command,
    apply_settle_proposal_approval,
)
from issue_orchestrator.domain.scoped_rework import TechLeadProposalCommand
from issue_orchestrator.domain.tech_lead_approval import (
    ApprovalTransition,
    ApprovalVerdictKind,
    with_proposal_marker,
)
from issue_orchestrator.ports.approval_evidence import (
    InMemoryOperatorApprovalRecords,
    InMemoryProposalIssueIndex,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

#: The real client class, before any test routes it to a fake GitHub.
_HTTPX_CLIENT = httpx.Client
REPO = "porchpin/porchpin"
PROPOSAL = 550
APP_ID = 4464000
CLIENT_ID = "Iv23liTESTclient"
SLUG = "porchpin-bot"
BOT_LOGIN = f"{SLUG}[bot]"
BOT_ID = 301493143
INSTALLATION_TOKEN = "installation-token"
PAT = "personal-token"
MAINTAINER = "BruceBGordon"
MAINTAINER_ID = 7084413


def _user(login: str, account_id: int, kind: str) -> dict:
    """An actor exactly as GitHub's issue events render one."""
    escaped = login.replace("[", "%5B").replace("]", "%5D")
    html = f"https://github.com/apps/{login.removesuffix('[bot]')}" if kind == "Bot" else f"https://github.com/{login}"
    return {
        "login": login, "id": account_id, "node_id": "BOT_kgDOEfhrlw" if kind == "Bot" else "MDQ6VXNlcjcwODQ0MTM=",
        "url": f"https://api.github.com/users/{escaped}", "html_url": html,
        "type": kind, "user_view_type": "public", "site_admin": False,
    }


@dataclass
class FakeGitHub:
    """One issue, its label events, and the identities behind each token."""

    #: Which account each bearer token writes as.
    writers: dict[str, dict] = field(default_factory=lambda: {
        INSTALLATION_TOKEN: _user(BOT_LOGIN, BOT_ID, "Bot"),
        PAT: _user(MAINTAINER, MAINTAINER_ID, "User"),
    })
    app: dict = field(default_factory=lambda: {"id": APP_ID, "client_id": CLIENT_ID, "slug": SLUG})
    users: dict[str, dict] = field(default_factory=lambda: {
        BOT_LOGIN: _user(BOT_LOGIN, BOT_ID, "Bot"),
    })
    labels: list[str] = field(default_factory=lambda: ["tech-lead-proposal", "awaiting-approval"])
    events: list[dict] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    _next_event: int = 32709419257

    def label_by(self, actor: dict, name: str, *, kind: str = "labeled") -> None:
        """A label transition, as GitHub's issue events list it."""
        self._next_event += 1
        self.events.append({
            "id": self._next_event, "node_id": "LE_x", "url": "u", "actor": actor, "event": kind,
            "commit_id": None, "commit_url": None, "created_at": f"2026-10-08T15:04:{len(self.events):02d}Z",
            "label": {"name": name, "color": "ededed"}, "performed_via_github_app": None,
        })
        if kind == "labeled" and name not in self.labels:
            self.labels.append(name)
        if kind == "unlabeled" and name in self.labels:
            self.labels.remove(name)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.reads.append(f"{request.method} {path}")
        token = request.headers.get("authorization", "").removeprefix("Bearer ").removeprefix("token ")
        issue = f"/repos/{REPO}/issues/{PROPOSAL}"
        if request.method == "POST" and path == "/app/installations/1/access_tokens":
            return httpx.Response(201, json={"token": INSTALLATION_TOKEN, "expires_at": "2099-01-01T00:00:00Z",
                                             "permissions": {"issues": "write"}})
        if path == "/app":
            assert token == "app-jwt", "GET /app authenticates as the App itself, not the installation"
            return httpx.Response(200, json=self.app)
        if path.startswith("/users/"):
            found = self.users.get(path.removeprefix("/users/"))
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if request.method == "GET" and path == issue:
            return httpx.Response(200, json={
                "number": PROPOSAL, "title": "Proposal", "state": "open",
                "labels": [{"name": name} for name in self.labels],
                "body": with_proposal_marker("Reset the retry budget of #450."),
            })
        if request.method == "POST" and path == f"{issue}/labels":
            for name in json.loads(request.content)["labels"]:
                if name not in self.labels:
                    self.label_by(self.writers[token], name)
            return httpx.Response(200, json=[{"name": name} for name in self.labels])
        if request.method == "DELETE" and path.startswith(f"{issue}/labels/"):
            self.label_by(self.writers[token], path.rsplit("/", 1)[1], kind="unlabeled")
            return httpx.Response(200, json=[{"name": name} for name in self.labels])
        if request.method == "GET" and path == f"{issue}/events":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.events if page == 1 else [])
        if path == f"{issue}/comments":
            if request.method == "POST":
                return httpx.Response(201, json={"id": 1, "html_url": "https://github.com/c/1"})
            return httpx.Response(200, json=[])
        if path.startswith(f"/repos/{REPO}/collaborators/"):
            login = path.split("/")[-2]
            if login == MAINTAINER:
                return httpx.Response(200, json={"permission": "admin", "role_name": "admin"})
            return httpx.Response(404, json={})
        raise AssertionError(f"unexpected GitHub request {request.method} {path}")


def _adapter(github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, *, app: bool) -> GitHubAdapter:
    """The production adapter stack, wired to *github*; an App engine or a
    personal-token one."""
    monkeypatch.setattr(httpx, "Client", lambda **kw: _HTTPX_CLIENT(transport=httpx.MockTransport(github.handle), **kw))
    auth = None
    if app:
        monkeypatch.setenv("TEST_APP_KEY", "unused")
        monkeypatch.setattr("issue_orchestrator.adapters.github.auth.jwt.encode", lambda *a, **k: "app-jwt")

        def call(method: str):
            return lambda url, *, headers, timeout: github.handle(httpx.Request(method, url, headers=headers))

        provider = GitHubAppInstallationTokenProvider(
            GitHubAppAuthConfig.from_values(
                client_id=CLIENT_ID, app_id=str(APP_ID), installation_id="1", private_key_env="TEST_APP_KEY"
            ),
            post=call("POST"), get=call("GET"),
        )
        auth = GitHubAuth(provider, (), repo=REPO)
    client = GitHubHttpClient(GitHubHttpConfig(repo=REPO, token=PAT, auth=auth))
    return GitHubAdapter(repo=REPO, http_client=client, verify_writes=False)


def _approvals(adapter: GitHubAdapter, records: InMemoryOperatorApprovalRecords) -> TechLeadApprovals:
    index = InMemoryProposalIssueIndex()
    index.index_proposals([PROPOSAL])
    return TechLeadApprovals(adapter, records, index, lambda: ())


def _approve(adapter: GitHubAdapter, approvals: TechLeadApprovals):
    return apply_operator_proposal_command(
        TechLeadProposalCommand(PROPOSAL, "approve"),
        repository=adapter, ops=InMemoryTechLeadAuthorityStore(), approvals=approvals,
        filtering_label=None, now=lambda: "2026-10-08T15:04:55+00:00",
    )


def _settle(adapter: GitHubAdapter, approvals: TechLeadApprovals, transition: ApprovalTransition):
    return apply_settle_proposal_approval(
        SettleProposalApprovalAction(issue_number=PROPOSAL, transition=transition),
        approvals=approvals, repository=adapter,
    )


def test_the_control_center_approve_binds_on_a_github_app_engine(monkeypatch) -> None:
    """#8987: the App's own `approved` (actor `porchpin-bot[bot]`, no
    `performed_via_github_app`) is recorded as the operator's approval."""
    github = FakeGitHub()
    adapter = _adapter(github, monkeypatch, app=True)
    records = InMemoryOperatorApprovalRecords()
    approvals = _approvals(adapter, records)

    outcome = _approve(adapter, approvals)

    assert outcome.outcome == "approved", outcome.detail
    written = github.events[-1]
    assert (written["actor"]["login"], written["actor"]["type"], written["performed_via_github_app"]) == (
        BOT_LOGIN, "Bot", None,
    )
    record = records.load_operator_approval(PROPOSAL)
    assert record is not None and record.label_event_id == written["id"]
    issue = adapter.get_issue(PROPOSAL)
    assert issue is not None
    verdict = approvals.verify(issue, fresh=True)
    assert (verdict.kind, verdict.actor) == (ApprovalVerdictKind.CONTROL_CENTER, BOT_LOGIN)


def test_the_next_tick_keeps_and_admits_a_control_center_approval(monkeypatch) -> None:
    """The live symptom's second half: settlement never strips the engine's
    own approval as automation, even after a restart re-reads everything."""
    github = FakeGitHub()
    records = InMemoryOperatorApprovalRecords()
    adapter = _adapter(github, monkeypatch, app=True)
    assert _approve(adapter, _approvals(adapter, records)).outcome == "approved"

    restarted = _adapter(github, monkeypatch, app=True)
    approvals = _approvals(restarted, records)

    assert _settle(restarted, approvals, ApprovalTransition.REJECT_CLAIM).details["settled"] == "unchanged"
    assert "approved" in github.labels
    assert _settle(restarted, approvals, ApprovalTransition.ADMIT).details["settled"] == "admitted"
    assert github.labels == ["tech-lead-proposal", "approved"]


def test_the_app_is_identified_once_and_by_its_own_jwt(monkeypatch) -> None:
    github = FakeGitHub()
    adapter = _adapter(github, monkeypatch, app=True)
    approvals = _approvals(adapter, InMemoryOperatorApprovalRecords())

    _approve(adapter, approvals)
    issue = adapter.get_issue(PROPOSAL)
    assert issue is not None
    assert approvals.verify(issue, fresh=True).approved

    assert github.reads.count("GET /app") == 1
    assert github.reads.count(f"GET /users/{BOT_LOGIN}") == 1


def test_another_apps_bot_is_never_the_engines_own_write(monkeypatch) -> None:
    """Only THIS App's bot account binds: a different App's `approved` in the
    standing run still refuses, and nothing stays recorded."""
    github = FakeGitHub()
    other = _user("renovate[bot]", 29139614, "Bot")
    github.label_by(other, "approved")
    adapter = _adapter(github, monkeypatch, app=True)
    records = InMemoryOperatorApprovalRecords()
    approvals = _approvals(adapter, records)
    issue = adapter.get_issue(PROPOSAL)
    assert issue is not None

    verdict = approvals.bind_engine_approval(PROPOSAL, recorded_at="t")

    assert (verdict.kind, verdict.actor) == (ApprovalVerdictKind.BOT_ACTOR, "renovate[bot]")
    assert records.load_operator_approval(PROPOSAL) is None


def test_a_bot_account_with_the_engines_login_but_another_id_is_not_its_write(monkeypatch) -> None:
    """The match is GitHub's immutable account id, not a login string."""
    github = FakeGitHub()
    github.label_by(_user(BOT_LOGIN, BOT_ID + 1, "Bot"), "approved")
    adapter = _adapter(github, monkeypatch, app=True)
    records = InMemoryOperatorApprovalRecords()

    verdict = _approvals(adapter, records).bind_engine_approval(PROPOSAL, recorded_at="t")

    assert verdict.kind is ApprovalVerdictKind.BOT_ACTOR
    assert records.load_operator_approval(PROPOSAL) is None


def test_the_legacy_migration_binding_attributes_the_apps_write(monkeypatch) -> None:
    """The migration's carried-over approval writes `approved` and binds it
    through the same owner call: the App's write is recorded there too."""
    github = FakeGitHub()
    adapter = _adapter(github, monkeypatch, app=True)
    records = InMemoryOperatorApprovalRecords()
    adapter.add_label(PROPOSAL, "approved")

    verdict = _approvals(adapter, records).bind_engine_approval(PROPOSAL, recorded_at="t")

    assert verdict.kind is ApprovalVerdictKind.CONTROL_CENTER
    record = records.load_operator_approval(PROPOSAL)
    assert record is not None and record.label_event_id == github.events[-1]["id"]


def test_a_personal_token_engine_approves_as_its_maintainer_user(monkeypatch) -> None:
    """A personal-token engine writes as its user (a `User`, a maintainer):
    nothing is recorded, and the label verifies as that maintainer's."""
    github = FakeGitHub()
    adapter = _adapter(github, monkeypatch, app=False)
    records = InMemoryOperatorApprovalRecords()
    approvals = _approvals(adapter, records)

    outcome = _approve(adapter, approvals)

    assert outcome.outcome == "approved", outcome.detail
    assert github.events[-1]["actor"]["login"] == MAINTAINER
    assert records.load_operator_approval(PROPOSAL) is None
    issue = adapter.get_issue(PROPOSAL)
    assert issue is not None
    assert approvals.verify(issue, fresh=True).kind is ApprovalVerdictKind.MAINTAINER
    assert "GET /app" not in github.reads


@pytest.mark.parametrize(
    "app,users,why",
    [
        ({"id": APP_ID + 1, "client_id": "Iv23liOTHER", "slug": SLUG}, None, "did not answer the configured App"),
        ({"id": APP_ID, "client_id": CLIENT_ID}, None, "did not include the App's slug"),
        (None, {BOT_LOGIN: _user(BOT_LOGIN, BOT_ID, "User")}, "not a Bot"),
        (None, {BOT_LOGIN: {**_user(BOT_LOGIN, BOT_ID, "Bot"), "login": "someone-else[bot]"}}, "for the App bot"),
        (None, {}, "404"),
    ],
    ids=["another-app", "no-slug", "not-a-bot-account", "another-login", "no-such-account"],
)
def test_an_unprovable_engine_identity_fails_the_approve_loudly(monkeypatch, app, users, why) -> None:
    """Fail closed AND loud: an engine that cannot prove its own bot account
    records nothing and says why, rather than refusing as automation."""
    github = FakeGitHub()
    if app is not None:
        github.app = app
    if users is not None:
        github.users = users
    adapter = _adapter(github, monkeypatch, app=True)
    records = InMemoryOperatorApprovalRecords()

    outcome = _approve(adapter, _approvals(adapter, records))

    assert outcome.outcome == "failed"
    assert why in outcome.detail
    assert records.load_operator_approval(PROPOSAL) is None


def test_a_misconfigured_app_identity_is_an_auth_error(monkeypatch) -> None:
    github = FakeGitHub()
    github.app = {"id": APP_ID + 1, "client_id": "Iv23liOTHER", "slug": "impostor"}
    adapter = _adapter(github, monkeypatch, app=True)

    github.label_by(_user(BOT_LOGIN, BOT_ID, "Bot"), "approved")
    standing = adapter.standing_label(PROPOSAL, "approved")
    assert standing is not None

    with pytest.raises(GitHubAuthError, match="did not answer the configured App"):
        adapter.is_own_write(standing.application)
