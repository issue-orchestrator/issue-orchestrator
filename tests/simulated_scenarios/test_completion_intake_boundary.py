"""Scenario composition preserves trusted intake even with a publishing double."""

import json
import shlex
from pathlib import Path

import pytest

from issue_orchestrator.domain.completion_intake import (
    CompletionValidationFailed,
    IntakeClosed,
)
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from tests.conftest import (
    MockEventSink,
    MockGitHubAdapter,
    build_test_orchestrator_deps,
)
from tests.simulated_scenarios.conftest import (
    FastScriptSessionRunner,
    StubWorkingCopy,
    TempWorktreeManager,
    build_config,
)
from tests.unit.test_completion_evidence_intake import command, completion


def configured_scenario_run(scenario_repo, validation_cmd):
    config = build_config(
        scenario_repo,
        coder_command="unused",
        reviewer_command="unused",
        validation_cmd=validation_cmd,
    )
    manager = TempWorktreeManager(scenario_repo)
    worktree = manager.create(scenario_repo, 1, "intake boundary").path
    real_git = GitWorkingCopy()
    deps = build_test_orchestrator_deps(
        config,
        MockGitHubAdapter(),
        MockEventSink(),
        FastScriptSessionRunner(),
        manager,
        working_copy=StubWorkingCopy(),
        intake_working_copy=real_git,
    )
    run = deps.issue_run_allocator.allocate(
        IssueRunAllocation(
            worktree_path=worktree,
            session_name="issue-1",
            issue_number=1,
            session_key=SessionKey(FakeIssueKey("1", config.repo), TaskKind.CODE),
            agent_label="agent:coder",
            backend="subprocess",
        )
    )
    return deps, run, real_git


@pytest.mark.parametrize("passed", [True, False])
def test_scenario_intake_executes_configured_validation_in_real_checkout(
    scenario_repo, passed
):
    deps, run, real_git = configured_scenario_run(
        scenario_repo,
        f"printf scenario-validator; exit {0 if passed else 1}",
    )
    worktree = run.worktree_path
    owner = deps.completion_intake
    capability = owner.submission_capability(run)
    # Invalid bytes and their corrected successor retain distinct custody.
    invalid = owner.submit(capability, command(b"invalid json", "invalid"))
    accepted = owner.submit(capability, command(completion(), "corrected"))
    owner.close_and_drain(1)
    assert owner.receipt_for_run(run) == accepted
    assert (
        deps.issue_run_ledger.entry_for_receipt(invalid.entry_id).raw_path.read_bytes()
        == b"invalid json"
    )
    attestation = deps.issue_run_ledger.validation_for_receipt(accepted.entry_id)
    assert attestation is not None
    assert attestation.passed is passed
    assert attestation.head_sha == real_git.get_head_sha(worktree)
    result = json.loads(attestation.result_path.read_bytes())
    assert Path(result["stdout_path"]).read_text() == "scenario-validator"
    assert not attestation.result_path.is_relative_to(worktree)
    if passed:
        owner.require_publication_ready(accepted, run)
    else:
        with pytest.raises(CompletionValidationFailed):
            owner.require_publication_ready(accepted, run)
    with pytest.raises(IntakeClosed):
        owner.submit(capability, command(b"late", "late"))


def test_fail_once_validation_state_survives_separate_isolated_attempts(scenario_repo):
    script = Path(__file__).parent / "fixtures/scripts/validate_fail_once.sh"
    state = scenario_repo / "validation-attempt-state"
    deps, run, _ = configured_scenario_run(
        scenario_repo,
        f"bash {shlex.quote(str(script))} {shlex.quote(str(state))}",
    )
    owner = deps.completion_intake
    capability = owner.submission_capability(run)
    results = []
    for attempt in (1, 2):
        receipt = owner.submit(
            capability, command(completion() + b" " * attempt, str(attempt))
        )
        owner.drain()
        result = deps.issue_run_ledger.validation_for_receipt(receipt.entry_id)
        assert result is not None
        results.append(result)
    owner.close_and_drain(1)
    assert [result.passed for result in results] == [False, True]
    assert results[0].result_path != results[1].result_path
    assert state.exists() and not state.is_relative_to(run.worktree_path)


def test_scenario_checkout_reuse_preserves_history_and_rejects_other_branch(
    scenario_repo,
):
    manager = TempWorktreeManager(scenario_repo)
    first = manager.create(scenario_repo, 1, "first")
    head = GitWorkingCopy().get_head_sha(first.path)
    operator_file = first.path / "uncommitted.txt"
    operator_file.write_text("preserve this")
    second = manager.create(scenario_repo, 1, "reuse")
    assert second == first
    assert GitWorkingCopy().get_head_sha(second.path) == head
    assert operator_file.read_text() == "preserve this"
    with pytest.raises(ValueError, match="different branch"):
        manager.create(scenario_repo, 1, "mismatch", branch_name="other")
    assert operator_file.read_text() == "preserve this"


@pytest.mark.parametrize("interrupted", [False, True])
def test_scripted_exchange_requires_fresh_receipts_and_drains_on_interruption(
    scenario_repo, interrupted
):
    from unittest.mock import Mock

    from issue_orchestrator.domain.completion_intake import CompletionIntakeError
    from issue_orchestrator.domain.models import AgentConfig
    from issue_orchestrator.domain.repository_launch_selection import (
        RepositoryLaunchSelection,
    )
    from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
    from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
    from tests.simulated_scenarios.exchange_coder import scripted_coder_attempts

    deps, run, _ = configured_scenario_run(scenario_repo, "printf validated")
    owner = deps.completion_intake
    capability = owner.submission_capability(run)
    commands = Mock(spec=CommandRunner)
    receipts = []

    def execute(_command, *, cwd, env, timeout_seconds):
        assert cwd == run.worktree_path
        receipts.append(
            owner.submit(
                env["ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY"], command(completion())
            )
        )
        if interrupted:
            raise RuntimeError("interrupted after submission")
        return CommandResult(0, "", "")

    commands.run.side_effect = execute
    config_path = scenario_repo / "default.yaml"
    config_path.write_text("{}")
    runtime = RuntimeConfigReference(
        config_path.resolve(),
        RepositoryLaunchSelection.parse(config_name=config_path.name),
    )
    agent = AgentConfig(prompt_path=config_path, command="scripted-coder")
    with pytest.raises(
        RuntimeError if interrupted else CompletionIntakeError,
        match="interrupted|missing new registered",
    ):
        with scripted_coder_attempts(
            owner, run, capability, agent, runtime, 12345, commands
        ) as attempt:
            attempt(1)
            # The transport retries the identical bytes; this cannot authorize a new attempt.
            attempt(2)
    assert receipts
    assert owner.receipt_for_run(run) == receipts[0]
    result = deps.issue_run_ledger.validation_for_receipt(receipts[0].entry_id)
    assert result is not None and result.passed
    with pytest.raises(IntakeClosed):
        owner.submit(capability, command(b"late", "late"))


@pytest.mark.parametrize("branch", ["1-sim", "27-rework"])
def test_publishing_double_reports_the_checked_out_branch(scenario_repo, branch):
    manager = TempWorktreeManager(scenario_repo)
    checkout = manager.create(scenario_repo, 1, "branch identity", branch_name=branch)
    working_copy = StubWorkingCopy()
    assert working_copy.get_current_branch(checkout.path) == checkout.branch_name
    assert working_copy.get_branch_status(checkout.path).branch == checkout.branch_name
    assert working_copy.push(checkout.path).branch == checkout.branch_name
    assert working_copy.list_branch_names(checkout.path) == [checkout.branch_name]
    assert working_copy.get_issue_number_from_branch(checkout.path) == int(
        branch.split("-")[0]
    )
    # The PR branch returned by publishing must be safe to reuse for rework.
    reused = manager.create(
        scenario_repo,
        1,
        "rework",
        branch_name=working_copy.get_current_branch(checkout.path),
    )
    assert reused == checkout
