"""Retained publication uses real custody, source checks, and completion policy."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_processor import LabelAdapter, PRAdapter
from issue_orchestrator.control.retained_completion_preparation import RetainedCompletionPreparation
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.domain.completion_processing import ProcessingResult
from issue_orchestrator.domain.recovery_publication import PreparedRecoveryPublication
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.validated_work import EvidenceRole, ReviewDisposition
from issue_orchestrator.execution.publication_workspace import EscrowPublicationWorkspaces
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.review_exchange_runner import ReviewExchangeRunner
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.run_allocation_helpers import make_completion_processor
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_validated_work_preservation import custody as custody


@pytest.fixture
def retained(custody):
    base = custody.git.head_sha(custody.repo)
    custody.git.run(custody.repo, ["update-ref", "refs/remotes/origin/main", base])
    (custody.worktree / ".gitignore").write_text(".issue-orchestrator/\n")
    custody.git.run(custody.worktree, ["add", ".gitignore"])
    custody.git.run(custody.worktree, ["commit", "-m", "Ignore run artifacts"])
    return custody


def prepare(rig, *, changed_validator=False, raw=None):
    raw = json.loads(completion()) if raw is None else raw
    raw["requested_actions"].append("create_pr")
    receipt = rig.intake.submit(rig.capability, command(json.dumps(raw).encode()))
    rig.intake.prepare_receipt_for_issue(receipt, rig.run, 42)
    rig.lifecycle.terminate(42, "retain before teardown")
    row = rig.store.retained_evidence(42)[0]
    rig.git.run(rig.repo, ["worktree", "remove", "--force", str(rig.worktree)])
    workspace_owner = EscrowPublicationWorkspaces(root=rig.escrow.root,
        repository=rig.repo, repo_slug="owner/repo", escrow=rig.escrow, git=rig.git)
    workspace = workspace_owner.prepare(row.admission)
    config = Config(repo="owner/repo", repo_root=rig.repo, worktree_base_branch_override="main")
    config.validation.quick.cmd = "false" if changed_validator else "true"
    config.validation.quick.timeout_seconds = 30
    exchange, prs, labels = Mock(spec=ReviewExchangeRunner), Mock(spec=PRAdapter), Mock(spec=LabelAdapter)
    processor = make_completion_processor(label_adapter=labels, pr_adapter=prs, git_adapter=rig.wc,
        session_output=FileSystemSessionOutput(), config=config, review_exchange_runner=exchange,
        agent_callback_endpoint=ready_callback_endpoint())
    owner = RetainedCompletionPreparation(intake=rig.ledger, completion=processor,
        working_copy=rig.wc, repo_slug="owner/repo")
    return SimpleNamespace(owner=owner, row=row, workspace=workspace, exchange=exchange,
        prs=prs, labels=labels, processor=processor)


def assert_no_effects(rig):
    assert rig.exchange.mock_calls == []
    assert rig.prs.mock_calls == []
    assert rig.labels.mock_calls == []


def test_removed_coding_worktree_still_prepares_exact_draft_with_recorded_role(retained):
    rig = prepare(retained)
    result = rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert isinstance(result, PreparedRecoveryPublication)
    assert result.command.target_head_sha == rig.row.admission.evidence.identity.key.validated_head_sha
    assert result.command.branch_name == "feature"
    assert result.command.content.draft
    assert "Retained feature" in result.command.content.title
    assert "change" in result.command.content.body
    assert result.processing_policy.agent_label == "agent:test"
    assert result.processing_policy.task is TaskKind.CODE
    assert result.review_disposition is ReviewDisposition.ROUTE_TO_PR_REVIEW
    assert result.completion.run.run == retained.run
    assert result.completion.run.run.worktree_path != rig.workspace.checkout
    assert not retained.worktree.exists()
    assert_no_effects(rig)


def test_changed_validation_configuration_requires_fresh_proof(retained):
    rig = prepare(retained, changed_validator=True)
    with pytest.raises(CompletionIntakeError, match="another configured validator"):
        rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert_no_effects(rig)


@pytest.mark.parametrize("kind", ["test-skip", "runtime-artifact"])
def test_retained_source_uses_normal_completion_guards(retained, kind):
    path = retained.worktree / ("tests/test_hidden.py" if kind == "test-skip" else ".issue-orchestrator/completion.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import pytest\n@pytest.mark.skip(reason='hidden')\ndef test_hidden():\n    assert False\n" if kind == "test-skip" else "{}")
    retained.git.run(retained.worktree, ["add", "--force", str(path)])
    retained.git.run(retained.worktree, ["commit", "-m", "Forbidden source"])
    rig = prepare(retained)
    result = rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert isinstance(result, ProcessingResult) and not result.success
    assert ("skip" if kind == "test-skip" else "runtime") in result.message.lower()
    assert_no_effects(rig)


def test_changed_publication_source_cannot_be_prepared(retained):
    rig = prepare(retained)
    (rig.workspace.checkout / "content").write_text("operator work")
    with pytest.raises(CompletionIntakeError, match="differs from its validated head"):
        rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert (rig.workspace.checkout / "content").read_text() == "operator work"
    assert_no_effects(rig)


def test_superseded_evidence_cannot_be_prepared(retained):
    rig = prepare(retained)
    row = replace(rig.row, role=EvidenceRole.SUPERSEDED)
    with pytest.raises(CompletionIntakeError, match="current exact repository evidence"):
        rig.owner.prepare(row, rig.workspace, "Retained feature")
    assert_no_effects(rig)


@pytest.mark.parametrize("field", ["repo", "evidence", "released"])
def test_unbound_or_released_evidence_is_rejected(retained, field):
    rig = prepare(retained)
    row, workspace = rig.row, rig.workspace
    if field == "repo":
        rig.owner = RetainedCompletionPreparation(intake=retained.ledger, completion=rig.processor,
            working_copy=retained.wc, repo_slug="other/repo")
    elif field == "evidence":
        workspace = replace(workspace, evidence_id="e1:" + "0" * 64)
    else:
        row = replace(row, released_at="2026-09-07T00:00:00Z")
    with pytest.raises(CompletionIntakeError, match="current exact repository evidence"):
        rig.owner.prepare(row, workspace, "Retained feature")
    assert_no_effects(rig)



def test_retained_record_cannot_request_the_configured_human_block(retained):
    from issue_orchestrator.control.needs_human_block import SharedNeedsHumanBlock
    raw = json.loads(completion())
    raw["pr_labels"] = ["operator-human-block"]
    rig = prepare(retained, raw=raw)
    block = Mock(spec=SharedNeedsHumanBlock)
    block.owns.side_effect = lambda label: label == "operator-human-block"
    rig.processor.needs_human_block = block
    result = rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert isinstance(result, ProcessingResult) and not result.success
    assert "reserved shared block" in result.message
    assert_no_effects(rig)


def test_create_pr_only_cannot_authorize_an_exact_push_to_master(retained):
    from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
    from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
    from issue_orchestrator.domain.issue_key import GitHubIssueKey
    from issue_orchestrator.domain.session_key import SessionKey
    retained.git.run(retained.worktree, ["branch", "-m", "master"])
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), retained.ledger, retained.wc,
        configuration=Config(repo="owner/repo"))
    retained.run = allocator.allocate(IssueRunAllocation(retained.worktree, "coding-master", 42,
        SessionKey(GitHubIssueKey("owner/repo", "42"), TaskKind.CODE), "agent:test", "test", terminal_id="issue-42"))
    retained.capability = retained.ledger.submission_capability(retained.run)
    raw = json.loads(completion())
    raw["requested_actions"] = []  # prepare adds CREATE_PR alone
    rig = prepare(retained, raw=raw)
    result = rig.owner.prepare(rig.row, rig.workspace, "Retained feature")
    assert isinstance(result, ProcessingResult) and not result.success
    assert "protected branch" in result.message
    assert_no_effects(rig)
