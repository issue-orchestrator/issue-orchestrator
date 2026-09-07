"""Reconciliation with production GitHub HTTP, verification, and durable ledger."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.tech_lead_case_file_reconciliation import CaseFileReconciliationPlan
from issue_orchestrator.control.tech_lead_case_files import build_pattern_ledger
from issue_orchestrator.entrypoints.cli_tech_lead import run_case_file_reconciliation
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.ports.verification import VerificationResult


class _SingleCheckVerification:
    """Exercise the real adapter's verifier, without retry sleeps."""

    def __init__(self):
        self.failed_targets = []

    def verify_condition(self, *, operation, target, check, budget):
        success, observed = check()
        if not success:
            self.failed_targets.append(target)
        return (VerificationResult.SUCCESS if success else VerificationResult.FAILED_FATAL), observed


class _Remote:
    def __init__(self, *, lost_response=False):
        self.comments = {10: [self.comment(i, "old discussion") for i in range(1, 101)], 20: []}
        self.state = "open"
        self.posts = []
        self.pages = []
        self.lost_response = lost_response

    @staticmethod
    def comment(number, body):
        return {"id": number, "body": body, "user": {"id": 7, "type": "User"},
                "html_url": f"https://github.com/owner/repo/issues/10#issuecomment-{number}"}

    def handle(self, request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 7})
        parts = request.url.path.split("/")
        number = int(parts[5])
        if parts[-1] == "comments":
            if request.method == "POST":
                body = json.loads(request.content)["body"]
                self.posts.append((number, body))
                comment = self.comment(1000 + len(self.posts), body)
                self.comments[number].append(comment)
                if self.lost_response:
                    self.lost_response = False
                    raise httpx.ReadError("response lost after publication")
                return httpx.Response(201, json=comment)
            page = int(request.url.params.get("page", "1"))
            self.pages.append((number, page))
            return httpx.Response(200, json=self.comments[number][(page - 1) * 100:page * 100])
        assert number == 20
        if request.method == "PATCH":
            self.state = json.loads(request.content)["state"]
        return httpx.Response(200, json={"number": number, "state": self.state, "labels": [], "title": "duplicate"})


class _Host:
    def __init__(self, authority, adapter):
        self.authority = authority
        self.adapter = adapter
        reader = MagicMock()
        reader.read_issue_labels.return_value = []
        self.applier = ActionApplier(
            labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
            repository_host=adapter, tech_lead_ops=authority,
            fresh_issue_reader=reader, reconcile=True,
        )

    def pattern_ledger(self):
        return build_pattern_ledger(self.authority.list_pattern_evidence())

    def issue_is_open(self, number):
        return self.adapter.get_dependency_issue_snapshot(number).state == "open"

    def apply(self, actions):
        return self.applier.apply_all(actions)


@pytest.mark.parametrize("failure", ["none", "before_record", "after_record", "lost_response"])
def test_cli_restart_recovers_paginated_evidence_without_reposting(tmp_path, monkeypatch, failure):
    remote = _Remote(lost_response=failure == "lost_response")
    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        transport=httpx.MockTransport(remote.handle), **kwargs,
    ))
    verification = _SingleCheckVerification()

    def new_host():
        client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="test-token"))
        adapter = GitHubAdapter(repo="owner/repo", http_client=client,
                                verify_writes=True, verification_service=verification)
        return _Host(SqliteTechLeadAuthorityStore.for_repo(tmp_path), adapter)

    plan_document = {"plan_id": "historical-receipts", "clusters": [{
        "signature": "repeated-failure", "tracker": 30, "summary": "Standing failure",
        "duplicates": [{"issue": 20, "note": "Source evidence: #20"}],
    }]}
    host = new_host()
    host.authority.record_pattern(signature="repeated-failure", issue_number=10, observation_id="original")
    original_note = host.authority.note_pattern_observation
    interrupted = False

    def note(**kwargs):
        nonlocal interrupted
        if not interrupted and failure in {"before_record", "after_record"}:
            interrupted = True
            if failure == "after_record":
                original_note(**kwargs)
            raise RuntimeError("local recording interrupted")
        original_note(**kwargs)

    with patch.object(host.authority, "note_pattern_observation", side_effect=note):
        exit_code = run_case_file_reconciliation(
            CaseFileReconciliationPlan.from_mapping(plan_document), host,
            config=Config(), apply_writes=True,
        )
    if failure in {"before_record", "after_record"}:
        assert exit_code == 1
        assert remote.state == "open"
    else:
        assert exit_code == 0
    # Recreate CLI plan, HTTP adapter, and SQLite owner; there is no in-memory
    # body or receipt from the first invocation available to the second.
    restarted = new_host()
    assert run_case_file_reconciliation(
        CaseFileReconciliationPlan.from_mapping(plan_document), restarted,
        config=Config(), apply_writes=True,
    ) == 0
    assert remote.state == "closed"
    assert restarted.pattern_ledger()["repeated-failure"].observation_count == 3
    assert len([number for number, _ in remote.posts if number == 10]) == 2
    assert len([number for number, _ in remote.posts if number == 20]) == 1
    assert (10, 2) in remote.pages
    assert "comment add #10" in verification.failed_targets
