"""Production HTTP/adapter boundary: exact content and credential provenance."""
import hashlib

import httpx
import pytest

from issue_orchestrator.adapters.github.auth import GitHubAppInstallationTokenProvider, GitHubAuth
from issue_orchestrator.adapters.github.tokens import GitHubAppAuthConfig
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.ports.repository_host import RepositoryHostError

BODY = "Diagnosis and remedy.\n<!-- io:receipt:test -->"


def _adapter(monkeypatch, handler, *, app=None):
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    auth = None
    if app:
        provider = GitHubAppInstallationTokenProvider(GitHubAppAuthConfig.from_values(
            app_id=app.get("id"), client_id=app.get("client_id"), installation_id="1", private_key_env="UNUSED_TEST_KEY"))
        monkeypatch.setattr(provider, "get_token", lambda: "test-app-token")
        auth = GitHubAuth(provider, ())
    client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="token", auth=auth))
    return GitHubAdapter(repo="owner/repo", http_client=client, verify_writes=False)


def _comment(body=BODY, user_id=7, **values):
    return {"body": body, "id": 55, "html_url": "https://github.com/owner/repo/issues/1#issuecomment-55",
        "user": {"id": user_id, "type": "User"}, **values}


@pytest.mark.parametrize("comment,accepted", [
    (_comment(), True), (_comment("<!-- io:receipt:test -->"), False),
    (_comment(user_id=8), False), (_comment("edited away\n<!-- io:receipt:test -->"), False),
])
def test_receipt_requires_exact_body_and_authenticated_author(monkeypatch, comment, accepted):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": 7} if request.url.path == "/user" else [comment])
    receipt = _adapter(monkeypatch, handler).find_issue_comment_receipt(1, body=BODY)
    assert (receipt is not None) is accepted
    assert all("if-none-match" not in request.headers for request in calls)
    if receipt:
        assert receipt.comment_id == "55" and receipt.author_key == "github-user:7"
        assert receipt.body_sha256 == hashlib.sha256(BODY.encode()).hexdigest()


@pytest.mark.parametrize("configured,observed,accepted", [
    ({"id": "12"}, {"id": 12}, True), ({"id": "12"}, {"id": 13}, False),
    ({"client_id": "Iv1.same"}, {"client_id": "Iv1.same"}, True),
    ({"id": "12", "client_id": "Iv1.active"}, {"id": 12, "client_id": "Iv1.other"}, False),
    ({"id": "12", "client_id": "Iv1.active"}, {"id": 13, "client_id": "Iv1.active"}, True),
    ({"id": "12", "client_id": "Iv1.active"}, {"id": 12, "client_id": "Iv1.active"}, True),
    ({"id": "12", "client_id": "Iv1.active"}, {"id": 12}, False),
])
def test_app_receipt_uses_server_app_provenance(monkeypatch, configured, observed, accepted):
    config = GitHubAppAuthConfig.from_values(app_id=configured.get("id"),
        client_id=configured.get("client_id"), installation_id="1", private_key_env="UNUSED_TEST_KEY")
    assert config.jwt_issuer == config.effective_identity.value
    assert config.effective_identity.matches(observed) is accepted
    def handler(request):
        assert request.url.path != "/user"
        return httpx.Response(200, json=[_comment(user={"id": 42, "type": "Bot"}, performed_via_github_app=observed)])
    receipt = _adapter(monkeypatch, handler, app=configured).find_issue_comment_receipt(1, body=BODY)
    assert (receipt is not None) is accepted


def test_receipt_reads_all_pages(monkeypatch):
    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7})
        page = request.url.params["page"]
        return httpx.Response(200, json=[_comment("other")] * 100 if page == "1" else [_comment()])
    assert _adapter(monkeypatch, handler).find_issue_comment_receipt(1, body=BODY) is not None


@pytest.mark.parametrize("payload", [{}, None, [None], [{"body": BODY}], [{"id": 55}]])
def test_malformed_receipt_scan_is_unknown(monkeypatch, payload):
    def handler(request):
        return httpx.Response(200, json={"id": 7} if request.url.path == "/user" else payload)
    with pytest.raises(RepositoryHostError):
        _adapter(monkeypatch, handler).find_issue_comment_receipt(1, body=BODY)


@pytest.mark.parametrize("payload", [{}, {"number": 6410}, [], None, {"state": "unexpected"}, {"state": []}])
def test_malformed_dependency_snapshot_is_unknown(monkeypatch, payload):
    host = _adapter(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(RepositoryHostError):
        host.get_dependency_issue_snapshot(6410)
