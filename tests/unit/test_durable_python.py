"""setup-guardrails never commits an interpreter path that will disappear (#8087).

``scripts/verify-pr.sh`` is committed by the target repo, so the interpreter it
prefers must outlive the checkout that generated it. These tests build real
git repositories with real linked worktrees in temp dirs; nothing touches a
real repository.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.entrypoints.cli import cmd_setup_guardrails
from issue_orchestrator.entrypoints import cli_support
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
from issue_orchestrator.infra.hooks.durable_python import (
    DurablePythonSource,
    UnstableInterpreterError,
    resolve_durable_orchestrator_python,
    unstable_interpreter_reason,
)
from issue_orchestrator.infra.repo_guardrails import (
    RepoGuardrailsError,
    VERIFY_PR_RELATIVE_PATH,
    inspect_repo_guardrails,
    managed_verify_preferred_python,
    setup_repo_guardrails,
)

NO_TEMP_ROOTS: tuple[Path, ...] = ()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def io_checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout and a linked worktree of it, each with a .venv python."""
    main = tmp_path / "io-main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init")
    linked = tmp_path / "worktrees" / "io-feature"
    _git(main, "worktree", "add", "-q", str(linked), "-b", "feature")
    _fake_python(main / ".venv" / "bin" / "python")
    _fake_python(linked / ".venv" / "bin" / "python")
    return main, linked


@pytest.fixture
def target_repo(tmp_path: Path) -> tuple[Path, Config]:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    config = Config(repo_root=repo)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=repo / "prompt.md", command="claude --print")
    }
    return repo, config


# --- the owner: resolve_durable_orchestrator_python ---------------------------


def test_linked_worktree_venv_is_unstable(io_checkouts):
    _main, linked = io_checkouts
    reason = unstable_interpreter_reason(
        linked / ".venv" / "bin" / "python", temp_roots=NO_TEMP_ROOTS
    )
    assert reason == f"it is inside the linked git worktree {linked}"


def test_main_checkout_venv_is_stable(io_checkouts):
    main, _linked = io_checkouts
    assert (
        unstable_interpreter_reason(main / ".venv" / "bin" / "python", temp_roots=NO_TEMP_ROOTS)
        is None
    )


def test_submodule_git_file_is_not_a_linked_worktree(tmp_path):
    """A ``.git`` file whose git dir has no ``commondir`` (a submodule) is stable."""
    checkout = tmp_path / "sub"
    gitdir = tmp_path / "super" / ".git" / "modules" / "sub"
    gitdir.mkdir(parents=True)
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
    python = _fake_python(checkout / ".venv" / "bin" / "python")
    assert unstable_interpreter_reason(python, temp_roots=NO_TEMP_ROOTS) is None


def test_temp_dir_interpreter_is_unstable(tmp_path):
    python = _fake_python(tmp_path / "venv" / "bin" / "python")
    reason = unstable_interpreter_reason(python, temp_roots=(tmp_path.resolve(),))
    assert reason is not None and "temporary directory" in reason


def test_default_temp_roots_cover_pytest_tmp_path(tmp_path):
    python = _fake_python(tmp_path / "venv" / "bin" / "python")
    reason = unstable_interpreter_reason(python)
    assert reason is not None and "temporary directory" in reason


def test_running_interpreter_in_linked_worktree_is_refused(io_checkouts):
    main, linked = io_checkouts
    with pytest.raises(UnstableInterpreterError) as excinfo:
        resolve_durable_orchestrator_python(
            None,
            environ={},
            running_interpreter=linked / ".venv" / "bin" / "python",
            temp_roots=NO_TEMP_ROOTS,
        )
    message = str(excinfo.value)
    assert "linked git worktree" in message
    assert "--python" in message and "ISSUE_ORCHESTRATOR_PYTHON" in message
    # The refusal points the operator at the stable interpreter it can use.
    assert str(main.resolve() / ".venv" / "bin" / "python") in message


def test_environment_override_in_linked_worktree_is_refused_not_skipped(io_checkouts):
    """A set but unstable ISSUE_ORCHESTRATOR_PYTHON never falls through silently."""
    main, linked = io_checkouts
    with pytest.raises(UnstableInterpreterError, match="ISSUE_ORCHESTRATOR_PYTHON"):
        resolve_durable_orchestrator_python(
            None,
            environ={"ISSUE_ORCHESTRATOR_PYTHON": str(linked / ".venv" / "bin" / "python")},
            running_interpreter=main / ".venv" / "bin" / "python",
            temp_roots=NO_TEMP_ROOTS,
        )


def test_explicit_python_wins_over_environment_and_running(io_checkouts):
    main, linked = io_checkouts
    stable = main / ".venv" / "bin" / "python"
    resolved = resolve_durable_orchestrator_python(
        stable,
        environ={"ISSUE_ORCHESTRATOR_PYTHON": str(linked / ".venv" / "bin" / "python")},
        running_interpreter=linked / ".venv" / "bin" / "python",
        temp_roots=NO_TEMP_ROOTS,
    )
    assert resolved.path == stable
    assert resolved.source is DurablePythonSource.EXPLICIT


def test_missing_or_relative_interpreter_is_refused(io_checkouts):
    main, _linked = io_checkouts
    with pytest.raises(UnstableInterpreterError, match="not an executable file"):
        resolve_durable_orchestrator_python(
            main / ".venv" / "bin" / "absent", environ={}, temp_roots=NO_TEMP_ROOTS
        )
    with pytest.raises(UnstableInterpreterError, match="absolute path"):
        resolve_durable_orchestrator_python(
            Path(".venv/bin/python"), environ={}, temp_roots=NO_TEMP_ROOTS
        )


# --- setup-guardrails: the acceptance scenario ---------------------------------


def test_setup_guardrails_from_linked_worktree_venv_writes_nothing(
    io_checkouts, target_repo, monkeypatch
):
    """The #8087 incident: run from a feature worktree's venv, nothing is written."""
    _main, linked = io_checkouts
    repo, config = target_repo
    worktree_python = linked / ".venv" / "bin" / "python"
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_PYTHON", raising=False)
    monkeypatch.setattr(sys, "executable", str(worktree_python))

    with pytest.raises(RepoGuardrailsError, match="linked git worktree"):
        setup_repo_guardrails(config)

    assert not (repo / VERIFY_PR_RELATIVE_PATH).exists()
    assert not (repo / ".githooks").exists()
    hooks_path = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo, capture_output=True, text=True,
    )
    assert hooks_path.stdout == ""


def test_cli_setup_guardrails_from_linked_worktree_venv_fails(
    io_checkouts, target_repo, monkeypatch, capsys
):
    _main, linked = io_checkouts
    repo, config = target_repo
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_PYTHON", raising=False)
    monkeypatch.setattr(sys, "executable", str(linked / ".venv" / "bin" / "python"))
    monkeypatch.setattr(cli_support, "load_config", lambda _args: config)
    args = argparse.Namespace(
        target=None, validation_cmd=None, hooks_dir=None, config=None, python=None
    )

    assert cmd_setup_guardrails(args) == 1
    assert "linked git worktree" in capsys.readouterr().out
    assert not (repo / VERIFY_PR_RELATIVE_PATH).exists()


def test_setup_guardrails_bakes_the_explicit_stable_python(target_repo):
    repo, config = target_repo
    stable = Path(sys.executable).resolve()

    setup_repo_guardrails(config, python=stable)

    content = (repo / VERIFY_PR_RELATIVE_PATH).read_text()
    assert managed_verify_preferred_python(content) == stable
    assert inspect_repo_guardrails(repo).verify_preferred_python == stable


def test_python_flag_is_rejected_for_the_portable_io_script(tmp_path):
    """io's own verify-pr.sh names no interpreter; a --python would be ignored."""
    repo = tmp_path / "io"
    (repo / "src" / "issue_orchestrator" / "entrypoints").mkdir(parents=True)
    (repo / "src" / "issue_orchestrator" / "entrypoints" / "cli.py").write_text("")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "pre-push").write_text("")
    _git(repo, "init", "-q")
    config = Config(repo_root=repo)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {}

    with pytest.raises(RepoGuardrailsError, match="machine-neutral"):
        setup_repo_guardrails(config, python=Path(sys.executable).resolve())


# --- doctor --------------------------------------------------------------------


def _bake(repo: Path, config: Config, python: Path) -> None:
    setup_repo_guardrails(config, python=Path(sys.executable).resolve())
    verify = repo / VERIFY_PR_RELATIVE_PATH
    stable = str(Path(sys.executable).resolve())
    verify.write_text(verify.read_text().replace(stable, str(python)))
    assert managed_verify_preferred_python(verify.read_text()) == python


def test_doctor_flags_committed_verify_script_with_missing_interpreter(
    target_repo, tmp_path
):
    repo, config = target_repo
    gone = tmp_path / "removed-worktree" / ".venv" / "bin" / "python"
    _bake(repo, config, gone)

    checks = hook_checks.check_repo_guardrails(config)

    assert checks[0].status == "error"
    assert f"prefers interpreter {gone}, which does not exist" in checks[0].detail


def test_doctor_warns_on_committed_verify_script_in_a_linked_worktree(
    io_checkouts, target_repo
):
    _main, linked = io_checkouts
    repo, config = target_repo
    worktree_python = linked / ".venv" / "bin" / "python"
    _bake(repo, config, worktree_python)

    checks = hook_checks.check_repo_guardrails(config)

    assert checks[0].status == "warning"
    assert "linked git worktree" in checks[0].detail
    assert os.fspath(worktree_python) in checks[0].detail


# --- review round 1 ------------------------------------------------------------


def test_stable_looking_symlink_into_a_linked_worktree_is_refused(
    io_checkouts, target_repo, tmp_path
):
    """F1: the symlink's own location is stable, but its target dies with the worktree."""
    _main, linked = io_checkouts
    repo, config = target_repo
    link = tmp_path / "stable-bin" / "python"
    link.parent.mkdir()
    link.symlink_to(linked / ".venv" / "bin" / "python")

    assert "linked git worktree" in (
        unstable_interpreter_reason(link, temp_roots=NO_TEMP_ROOTS) or ""
    )
    with pytest.raises(RepoGuardrailsError, match="linked git worktree"):
        setup_repo_guardrails(config, python=link)
    assert not (repo / VERIFY_PR_RELATIVE_PATH).exists()


def test_nested_repository_inside_a_linked_worktree_is_refused(
    io_checkouts, target_repo
):
    """F2: a nested repo's own .git dir does not hide the enclosing linked worktree."""
    _main, linked = io_checkouts
    repo, config = target_repo
    nested = linked / "vendor" / "tools"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    python = _fake_python(nested / ".venv" / "bin" / "python")

    assert unstable_interpreter_reason(python, temp_roots=NO_TEMP_ROOTS) == (
        f"it is inside the linked git worktree {linked}"
    )
    with pytest.raises(RepoGuardrailsError, match="linked git worktree"):
        setup_repo_guardrails(config, python=python)


def test_session_scoped_worktree_hook_is_rebaked_on_every_install(
    io_checkouts, tmp_path, monkeypatch
):
    """Why the per-worktree hook keeps the running engine's interpreter.

    Runtime setup re-applies ``install_hooks`` on every session launch in a
    reused worktree, so a hook baked by an engine that has since been replaced
    is rewritten with the current engine's interpreter before that worktree's
    next orchestrator push.
    """
    from issue_orchestrator.adapters.worktree._worktree_hooks import install_hooks

    main, _linked = io_checkouts
    agent_worktree = tmp_path / "agent-wt"
    _git(main, "worktree", "add", "-q", str(agent_worktree), "-b", "agent")
    old_engine = _fake_python(tmp_path / "old-engine" / ".venv" / "bin" / "python")
    new_engine = _fake_python(tmp_path / "new-engine" / ".venv" / "bin" / "python")

    def baked_hooks() -> str:
        hooks = (main / ".git" / "worktrees" / "agent-wt").rglob("pre-push*")
        return "\n".join(path.read_text() for path in hooks if path.is_file())

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_PYTHON", str(old_engine))
    assert install_hooks(agent_worktree)
    assert str(old_engine) in baked_hooks()

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_PYTHON", str(new_engine))
    assert install_hooks(agent_worktree)
    rebaked = baked_hooks()
    assert str(new_engine) in rebaked
    assert str(old_engine) not in rebaked


def test_symlink_chain_through_a_linked_worktree_is_refused(
    io_checkouts, target_repo, tmp_path
):
    """Round 2 F1: stable start and stable end, but a middle link dies with the worktree."""
    _main, linked = io_checkouts
    repo, config = target_repo
    real = Path(sys.executable).resolve()
    middle = linked / "bin" / "python"
    middle.parent.mkdir()
    middle.symlink_to(real)
    outer = tmp_path / "stable-bin" / "python"
    outer.parent.mkdir()
    outer.symlink_to(middle)

    assert unstable_interpreter_reason(outer, temp_roots=NO_TEMP_ROOTS) == (
        f"it is inside the linked git worktree {linked}"
    )
    with pytest.raises(RepoGuardrailsError, match="linked git worktree"):
        setup_repo_guardrails(config, python=outer)
    assert not (repo / VERIFY_PR_RELATIVE_PATH).exists()


def test_dotdot_through_a_directory_link_into_a_worktree_is_refused(
    io_checkouts, tmp_path
):
    """Round 3 F1: ``alias/..`` is applied after following ``alias``, as the kernel does.

    ``outer -> stable/alias/../middle`` where ``alias`` points into a linked
    worktree: opening ``outer`` traverses the worktree, so it must be refused
    even though a lexical normalisation would drop ``alias/..``.
    """
    _main, linked = io_checkouts
    inner = linked / "inner"
    inner.mkdir()
    (linked / "middle").symlink_to(Path(sys.executable).resolve())
    stable = tmp_path / "stable"
    stable.mkdir()
    (stable / "alias").symlink_to(inner)
    outer = tmp_path / "outer-python"
    outer.symlink_to(stable / "alias" / ".." / "middle")
    assert outer.resolve() == Path(sys.executable).resolve()

    reason = unstable_interpreter_reason(outer, temp_roots=NO_TEMP_ROOTS)

    assert reason == f"it is inside the linked git worktree {linked}"


def test_symlink_loop_is_unstable_not_a_crash(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    reason = unstable_interpreter_reason(loop, temp_roots=NO_TEMP_ROOTS)
    assert reason is not None and "too many symbolic links" in reason


def test_interpreter_path_with_a_newline_is_refused(io_checkouts):
    """Round 3 F2: a quoted multi-line path could not be read back by doctor."""
    main, _linked = io_checkouts
    odd = _fake_python(main / "odd\ndir" / "python")
    with pytest.raises(UnstableInterpreterError, match="control character"):
        resolve_durable_orchestrator_python(odd, environ={}, temp_roots=NO_TEMP_ROOTS)
