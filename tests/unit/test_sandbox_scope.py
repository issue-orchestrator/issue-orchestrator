"""Unit tests for the domain sandbox scope value object and policy (ADR-0034)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.sandbox_scope import (
    DEFAULT_SANDBOX_DENY_ENV,
    DEFAULT_SANDBOX_DENY_READ_FILES,
    REVIEW_EXCHANGE_CODER_TASK_KIND,
    REVIEW_EXCHANGE_REVIEWER_TASK_KIND,
    SandboxScope,
    SandboxScopeContext,
    compute_session_scope,
)


def _agent(*, sandbox: bool, provider: str | None = "claude-code") -> AgentConfig:
    return AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider=provider,
        model="sonnet",
        sandbox=sandbox,
    )


def _ctx(
    task_kind: str = "code",
    worktree: Path = Path("/wt/issue-1"),
    evidence_read_roots: tuple[Path, ...] = (),
) -> SandboxScopeContext:
    return SandboxScopeContext(
        task_kind=task_kind, worktree=worktree, evidence_read_roots=evidence_read_roots
    )


# ---------------------------------------------------------------------------
# SandboxScope value object
# ---------------------------------------------------------------------------


def test_sandbox_scope_is_frozen_value_object() -> None:
    scope = SandboxScope(
        working_directory=Path("/wt"),
        read_roots=(Path("/wt"),),
        write_roots=(Path("/wt"),),
        egress="model-only",
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )
    with pytest.raises((AttributeError, TypeError)):
        scope.egress = "model+web"  # type: ignore[misc]


def test_sandbox_scope_rejects_empty_read_roots() -> None:
    with pytest.raises(ValueError, match="read_roots must not be empty"):
        SandboxScope(
            working_directory=Path("/wt"),
            read_roots=(),
            write_roots=(),
            egress="none",
            deny_env=(),
            deny_read_files=(),
        )


def test_sandbox_scope_rejects_unknown_egress() -> None:
    with pytest.raises(ValueError, match="egress must be one of"):
        SandboxScope(
            working_directory=Path("/wt"),
            read_roots=(Path("/wt"),),
            write_roots=(Path("/wt"),),
            egress="everything",  # type: ignore[arg-type]
            deny_env=(),
            deny_read_files=(),
        )


def test_sandbox_scope_requires_explicit_working_directory_in_both_roots() -> None:
    with pytest.raises(ValueError, match="working_directory.*write_roots"):
        SandboxScope(
            working_directory=Path("/wt"),
            read_roots=(Path("/wt"),),
            write_roots=(Path("/scratch"),),
            egress="model-only",
            deny_env=(),
            deny_read_files=(),
        )


# ---------------------------------------------------------------------------
# compute_session_scope — opt-in gate
# ---------------------------------------------------------------------------


def test_not_opted_in_returns_none() -> None:
    assert compute_session_scope(_agent(sandbox=False), _ctx()) is None


def test_opted_in_coder_scope() -> None:
    worktree = Path("/wt/issue-42")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("code", worktree))

    assert scope is not None
    assert scope.working_directory == worktree
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"
    assert scope.deny_env == DEFAULT_SANDBOX_DENY_ENV
    assert "GITHUB_TOKEN" in scope.deny_env
    # Fail-closed secret paths are populated from the domain denylist.
    assert scope.deny_read_files == DEFAULT_SANDBOX_DENY_READ_FILES
    assert "~/.ssh" in scope.deny_read_files
    assert "~/.issue-orchestrator" in scope.deny_read_files


def test_opted_in_rework_is_coder_scope() -> None:
    worktree = Path("/wt/issue-7")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("rework", worktree))
    assert scope is not None
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


@pytest.mark.parametrize("task_kind", ["review", "retrospective-review"])
def test_opted_in_reviewer_scope(task_kind: str) -> None:
    worktree = Path("/wt/issue-9")
    scope = compute_session_scope(_agent(sandbox=True), _ctx(task_kind, worktree))

    assert scope is not None
    # Reviewer reads and writes its own worktree (it runs builds/tests there).
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


def test_opted_in_tech_lead_without_evidence_stays_worktree_bounded() -> None:
    # No evidence roots (e.g. a health review with none staged): still bounded to
    # the worktree, never left unsandboxed.
    worktree = Path("/wt/issue-3")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("tech_lead", worktree))
    assert scope is not None
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)


def test_opted_in_tech_lead_reads_evidence_god_view_but_writes_only_worktree() -> None:
    # R5 (#6824): a tech-lead failure investigation READS the evidence-map god-view
    # roots (main repo, registered worktrees, state dbs, run-dirs) while WRITES
    # stay confined to the scratch worktree.
    worktree = Path("/wt/repo-tech-lead-42-abc")
    god_view = (Path("/repo"), Path("/repo/.issue-orchestrator/state"), Path("/wt/repo-7"))
    scope = compute_session_scope(
        _agent(sandbox=True), _ctx("tech_lead", worktree, evidence_read_roots=god_view)
    )
    assert scope is not None
    assert scope.read_roots == (worktree, *god_view)  # worktree first, then god-view
    assert scope.write_roots == (worktree,)  # writes never widen
    assert scope.egress == "model-only"


@pytest.mark.parametrize(
    "task_kind",
    [REVIEW_EXCHANGE_CODER_TASK_KIND, REVIEW_EXCHANGE_REVIEWER_TASK_KIND],
)
def test_review_exchange_task_kinds_are_recognized_roles(task_kind: str) -> None:
    # The persistent review-exchange launches with per-role task kinds; the
    # policy must resolve them explicitly (a bounded worktree scope) rather than
    # relying on the unknown-task CODER fail-safe.
    worktree = Path("/wt/issue-13")
    scope = compute_session_scope(_agent(sandbox=True), _ctx(task_kind, worktree))
    assert scope is not None
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


def test_unknown_task_kind_fails_safe_to_bounded_scope() -> None:
    worktree = Path("/wt/issue-5")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("mystery", worktree))
    assert scope is not None
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


def test_opt_in_independent_of_provider() -> None:
    # The scope is provider-agnostic: a codex agent that opts in still gets a
    # scope (the codex *translation* is what is deferred, not the policy).
    worktree = Path("/wt/issue-11")
    scope = compute_session_scope(_agent(sandbox=True, provider="codex"), _ctx("code", worktree))
    assert scope is not None
