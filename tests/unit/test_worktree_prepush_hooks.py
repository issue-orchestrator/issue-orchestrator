"""Regression tests for worktree pre-push hook installation.

These tests cover the interaction between the repo-level ``setup-guardrails``
pre-push wrapper and the per-worktree hook installer. The specific bug this
guards against: the repo wrapper running ``pre-push.project`` by path, while
the worktree installer copies the repo wrapper *into* that path — yielding
infinite recursion and a forkbombed push.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree._worktree_hooks import (
    _chained_hook_script,
    _install_chained_hook,
    _resolve_project_pre_push_hook,
    install_hooks,
)
from issue_orchestrator.infra.repo_guardrails import (
    LEGACY_MANAGED_PRE_PUSH_MARKER,
    MANAGED_PRE_PUSH_MARKER,
)


def _make_managed_wrapper() -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"# {MANAGED_PRE_PUSH_MARKER}\n"
        '"$HOOK_DIR/pre-push.project" "$@"\n'
    )


def _make_legacy_managed_wrapper() -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"# {LEGACY_MANAGED_PRE_PUSH_MARKER}\n"
        '"$HOOK_DIR/pre-push.project" "$@"\n'
    )


def _make_real_project_hook() -> str:
    return "#!/usr/bin/env bash\necho real-project-hook\nexit 0\n"


def _make_project_prepare_commit_msg_hook() -> str:
    return "#!/bin/sh\necho prepared >> \"$1\"\n"


def _make_project_applypatch_msg_hook() -> str:
    return "#!/bin/sh\nexec \"$(dirname \"$0\")/prepare-commit-msg\" \"$@\"\n"


def _init_main_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Initialise a bare-ish main repo with a guardrailed .githooks/ and a worktree.

    Returns (main_repo_root, worktree_path, worktree_hooks_dir).
    """
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    subprocess.run(["git", "init"], cwd=main_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    (main_repo / "file").write_text("seed\n")
    subprocess.run(
        ["git", "add", "file"], cwd=main_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=main_repo, check=True, capture_output=True
    )

    # Simulate a guardrailed repo: .githooks/pre-push is the managed wrapper, and
    # .githooks/pre-push.project holds the real project hook (what a user
    # actually wants to run on pre-push).
    githooks = main_repo / ".githooks"
    githooks.mkdir()
    (githooks / "pre-push").write_text(_make_managed_wrapper())
    (githooks / "pre-push").chmod(0o755)
    (githooks / "pre-push.project").write_text(_make_real_project_hook())
    (githooks / "pre-push.project").chmod(0o755)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", ".githooks"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "wt-feature"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "feature"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    worktree_hooks = main_repo / ".git" / "worktrees" / "wt-feature" / "hooks"
    return main_repo, worktree_path, worktree_hooks


def test_resolve_project_pre_push_hook_skips_managed_wrapper(tmp_path: Path) -> None:
    main_repo, _, _ = _init_main_repo_with_worktree(tmp_path)
    gitdir = main_repo / ".git" / "worktrees" / "wt-feature"

    resolved = _resolve_project_pre_push_hook(gitdir, ".githooks")

    assert resolved == main_repo / ".githooks" / "pre-push.project"
    assert resolved.read_text() == _make_real_project_hook()


def test_resolve_project_pre_push_hook_skips_legacy_managed_wrapper(
    tmp_path: Path,
) -> None:
    main_repo, _, _ = _init_main_repo_with_worktree(tmp_path)
    (main_repo / ".githooks" / "pre-push").write_text(_make_legacy_managed_wrapper())
    gitdir = main_repo / ".git" / "worktrees" / "wt-feature"

    resolved = _resolve_project_pre_push_hook(gitdir, ".githooks")

    assert resolved == main_repo / ".githooks" / "pre-push.project"
    assert resolved.read_text() == _make_real_project_hook()


def test_resolve_returns_none_when_managed_wrapper_has_no_project_sibling(
    tmp_path: Path,
) -> None:
    main_repo, _, _ = _init_main_repo_with_worktree(tmp_path)
    # Remove the real project hook; only the managed wrapper remains.
    (main_repo / ".githooks" / "pre-push.project").unlink()
    gitdir = main_repo / ".git" / "worktrees" / "wt-feature"

    resolved = _resolve_project_pre_push_hook(gitdir, ".githooks")

    assert resolved is None


def test_resolve_returns_none_when_no_pre_push_exists(tmp_path: Path) -> None:
    main_repo, _, _ = _init_main_repo_with_worktree(tmp_path)
    (main_repo / ".githooks" / "pre-push").unlink()
    (main_repo / ".githooks" / "pre-push.project").unlink()
    gitdir = main_repo / ".git" / "worktrees" / "wt-feature"

    assert _resolve_project_pre_push_hook(gitdir, ".githooks") is None


def test_install_hooks_in_guardrailed_worktree_does_not_recurse(tmp_path: Path) -> None:
    main_repo, worktree_path, worktree_hooks = _init_main_repo_with_worktree(tmp_path)

    install_hooks(worktree_path)

    project_hook = worktree_hooks / "pre-push.project"
    assert project_hook.exists()
    assert project_hook.read_text() == _make_real_project_hook()
    assert MANAGED_PRE_PUSH_MARKER not in project_hook.read_text(), (
        "worktree pre-push.project must NOT be the managed wrapper"
    )
    pre_push = worktree_hooks / "pre-push"
    assert pre_push.exists()
    assert pre_push.stat().st_mode & stat.S_IXUSR


def test_install_hooks_preserves_project_commit_message_hooks(
    tmp_path: Path,
) -> None:
    main_repo, worktree_path, worktree_hooks = _init_main_repo_with_worktree(tmp_path)
    prepare_commit_msg = main_repo / ".githooks" / "prepare-commit-msg"
    applypatch_msg = main_repo / ".githooks" / "applypatch-msg"
    prepare_commit_msg.write_text(_make_project_prepare_commit_msg_hook())
    prepare_commit_msg.chmod(0o755)
    applypatch_msg.write_text(_make_project_applypatch_msg_hook())
    applypatch_msg.chmod(0o755)

    install_hooks(worktree_path)

    installed_prepare = worktree_hooks / "prepare-commit-msg"
    installed_applypatch = worktree_hooks / "applypatch-msg"
    assert installed_prepare.read_text() == _make_project_prepare_commit_msg_hook()
    assert installed_applypatch.read_text() == _make_project_applypatch_msg_hook()
    assert installed_prepare.stat().st_mode & stat.S_IXUSR
    assert installed_applypatch.stat().st_mode & stat.S_IXUSR


def test_install_hooks_quarantines_pre_existing_corrupt_project_hook(
    tmp_path: Path,
) -> None:
    main_repo, worktree_path, worktree_hooks = _init_main_repo_with_worktree(tmp_path)
    worktree_hooks.mkdir(parents=True, exist_ok=True)
    corrupt = worktree_hooks / "pre-push.project"
    corrupt.write_text(_make_managed_wrapper())
    corrupt.chmod(0o755)

    install_hooks(worktree_path)

    # The corrupt file must be renamed; the new project hook must be the real
    # one from the main repo. Nothing points at the managed wrapper.
    assert corrupt.read_text() == _make_real_project_hook()
    quarantined = list(worktree_hooks.glob("pre-push.project.quarantined-*"))
    assert len(quarantined) == 1
    assert MANAGED_PRE_PUSH_MARKER in quarantined[0].read_text()


def test_install_chained_hook_raises_on_managed_wrapper_as_project_hook(
    tmp_path: Path,
) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    bogus_project_hook = tmp_path / "pre-push"
    bogus_project_hook.write_text(_make_managed_wrapper())
    bogus_project_hook.chmod(0o755)
    orchestrator_hook = tmp_path / "orchestrator"
    orchestrator_hook.write_text("#!/usr/bin/env bash\nexit 0\n")
    orchestrator_hook.chmod(0o755)

    with pytest.raises(RuntimeError, match="Refusing to install managed wrapper"):
        _install_chained_hook(
            hooks_dir,
            hooks_dir / "pre-push",
            bogus_project_hook,
            orchestrator_hook,
        )

    # The installer refused before writing anything — silently running only
    # the orchestrator chain (and dropping the repo's lint/test gate) would
    # be a worse failure than worktree creation erroring loudly.
    assert not (hooks_dir / "pre-push").exists()
    assert not (hooks_dir / "pre-push.project").exists()


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_chained_wrapper_refuses_managed_project_hook(harness: Path) -> None:
    hooks_dir = harness / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-push").write_text(_chained_hook_script())
    (hooks_dir / "pre-push").chmod(0o755)
    (hooks_dir / "pre-push.project").write_text(_make_managed_wrapper())
    (hooks_dir / "pre-push.project").chmod(0o755)

    result = subprocess.run(
        [str(hooks_dir / "pre-push")],
        cwd=harness,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        timeout=30,
    )

    assert result.returncode != 0, "wrapper must refuse to exec managed project hook"
    assert "refusing to recurse" in result.stderr.lower()
    audit_log = (hooks_dir / "pre-push.log").read_text()
    assert "recursion guard" in audit_log.lower()


def test_chained_wrapper_refuses_legacy_managed_project_hook(harness: Path) -> None:
    hooks_dir = harness / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-push").write_text(_chained_hook_script())
    (hooks_dir / "pre-push").chmod(0o755)
    (hooks_dir / "pre-push.project").write_text(_make_legacy_managed_wrapper())
    (hooks_dir / "pre-push.project").chmod(0o755)

    result = subprocess.run(
        [str(hooks_dir / "pre-push")],
        cwd=harness,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        timeout=30,
    )

    assert result.returncode != 0, "wrapper must refuse to exec managed project hook"
    assert "refusing to recurse" in result.stderr.lower()
    audit_log = (hooks_dir / "pre-push.log").read_text()
    assert "recursion guard" in audit_log.lower()


def test_chained_wrapper_ignores_skip_project_hook_env_var(harness: Path) -> None:
    """Regression for security issue #5987 (F5).

    ``ORCHESTRATOR_SKIP_PROJECT_HOOK=1`` used to disable the project hook. A
    compromised agent in the worktree could set it and neuter the project's
    lint/test gate. The wrapper must run the project hook regardless.
    """
    hooks_dir = harness / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-push").write_text(_chained_hook_script())
    (hooks_dir / "pre-push").chmod(0o755)
    marker = harness / "project-hook-ran"
    (hooks_dir / "pre-push.project").write_text(
        f"#!/usr/bin/env bash\ntouch {marker}\nexit 0\n"
    )
    (hooks_dir / "pre-push.project").chmod(0o755)

    result = subprocess.run(
        [str(hooks_dir / "pre-push")],
        cwd=harness,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": os.environ.get("PATH", ""),
            "ORCHESTRATOR_SKIP_PROJECT_HOOK": "1",
        },
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "project hook must run even when skip env var is set"


def test_chained_wrapper_runs_benign_project_hook(harness: Path) -> None:
    hooks_dir = harness / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-push").write_text(_chained_hook_script())
    (hooks_dir / "pre-push").chmod(0o755)
    marker = harness / "project-hook-ran"
    (hooks_dir / "pre-push.project").write_text(
        f"#!/usr/bin/env bash\ntouch {marker}\nexit 0\n"
    )
    (hooks_dir / "pre-push.project").chmod(0o755)

    result = subprocess.run(
        [str(hooks_dir / "pre-push")],
        cwd=harness,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
