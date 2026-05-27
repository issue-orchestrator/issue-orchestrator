"""Tests for the tracked DCO prepare-commit-msg hook."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / ".githooks"
HOOK_PATH = HOOKS_DIR / "prepare-commit-msg"
APPLYPATCH_HOOK_PATH = HOOKS_DIR / "applypatch-msg"


def _git_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "global-gitconfig")
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env.pop("GIT_AUTHOR_NAME", None)
    env.pop("GIT_AUTHOR_EMAIL", None)
    env.pop("GIT_COMMITTER_NAME", None)
    env.pop("GIT_COMMITTER_EMAIL", None)
    env.pop("EMAIL", None)
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["XDG_CONFIG_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def _git_env_with_author_identity(
    env: dict[str, str],
    *,
    name: str,
    email: str,
) -> dict[str, str]:
    commit_env = env.copy()
    commit_env["GIT_AUTHOR_NAME"] = name
    commit_env["GIT_AUTHOR_EMAIL"] = email
    return commit_env


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _init_repo(
    tmp_path: Path,
    *,
    configure_identity: bool = True,
) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env(tmp_path)
    _git(repo, "init", "-q", env=env)
    _git(repo, "config", "core.hooksPath", str(HOOKS_DIR), env=env)
    if configure_identity:
        _git(repo, "config", "user.name", "Dev User", env=env)
        _git(repo, "config", "user.email", "dev@example.com", env=env)
    return repo, env


def _commit_file(
    repo: Path,
    env: dict[str, str],
    *,
    content: str,
    message_args: list[str],
    filename: str = "tracked.txt",
) -> subprocess.CompletedProcess[str]:
    path = repo / filename
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", filename, env=env)
    return _git(repo, "commit", *message_args, env=env, check=False)


def _last_commit_message(repo: Path, env: dict[str, str]) -> str:
    result = _git(repo, "log", "-1", "--format=%B", env=env)
    return result.stdout


def _head_author(repo: Path, env: dict[str, str]) -> str:
    result = _git(repo, "log", "-1", "--format=%an <%ae>", env=env)
    return result.stdout.strip()


def _head_signoff_lines(repo: Path, env: dict[str, str]) -> list[str]:
    return [
        line
        for line in _last_commit_message(repo, env).splitlines()
        if line.startswith("Signed-off-by: ")
    ]


def _assert_head_author_is_signed_off(repo: Path, env: dict[str, str]) -> None:
    assert f"Signed-off-by: {_head_author(repo, env)}" in _head_signoff_lines(
        repo,
        env,
    )


def test_prepare_commit_msg_adds_dco_signoff_from_config_author_identity(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path)

    result = _commit_file(repo, env, content="first\n", message_args=["-m", "Unsigned"])

    assert result.returncode == 0, result.stderr
    _assert_head_author_is_signed_off(repo, env)


def test_prepare_commit_msg_signs_author_when_author_env_differs_from_committer(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path)

    result = _commit_file(
        repo,
        _git_env_with_author_identity(
            env,
            name="Author Env",
            email="author-env@example.com",
        ),
        content="first\n",
        message_args=["-m", "Divergent author"],
    )

    assert result.returncode == 0, result.stderr
    assert _head_author(repo, env) == "Author Env <author-env@example.com>"
    _assert_head_author_is_signed_off(repo, env)
    assert "Signed-off-by: Dev User <dev@example.com>" not in _head_signoff_lines(
        repo,
        env,
    )


def test_prepare_commit_msg_signs_author_from_commit_author_option(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path)

    result = _commit_file(
        repo,
        env,
        content="first\n",
        message_args=[
            "--author",
            "Flag Author <flag-author@example.com>",
            "-m",
            "Flag author",
        ],
    )

    assert result.returncode == 0, result.stderr
    assert _head_author(repo, env) == "Flag Author <flag-author@example.com>"
    _assert_head_author_is_signed_off(repo, env)


def test_prepare_commit_msg_signs_cherry_picked_author(tmp_path: Path) -> None:
    repo, env = _init_repo(tmp_path)
    result = _commit_file(repo, env, content="base\n", message_args=["-m", "Base"])
    assert result.returncode == 0, result.stderr
    base_branch = _git(repo, "branch", "--show-current", env=env).stdout.strip()

    _git(repo, "switch", "-c", "foreign-author", env=env)
    _git(repo, "config", "core.hooksPath", "/dev/null", env=env)
    result = _commit_file(
        repo,
        _git_env_with_author_identity(
            env,
            name="Cherry Author",
            email="cherry-author@example.com",
        ),
        content="foreign\n",
        message_args=["-m", "Foreign change"],
    )
    assert result.returncode == 0, result.stderr
    foreign_sha = _git(repo, "rev-parse", "HEAD", env=env).stdout.strip()

    _git(repo, "switch", base_branch, env=env)
    _git(repo, "config", "core.hooksPath", str(HOOKS_DIR), env=env)
    result = _git(repo, "cherry-pick", foreign_sha, env=env, check=False)

    assert result.returncode == 0, result.stderr
    assert _head_author(repo, env) == "Cherry Author <cherry-author@example.com>"
    _assert_head_author_is_signed_off(repo, env)


def test_applypatch_msg_signs_git_am_author(tmp_path: Path) -> None:
    repo, env = _init_repo(tmp_path)
    result = _commit_file(repo, env, content="base\n", message_args=["-m", "Base"])
    assert result.returncode == 0, result.stderr

    source = tmp_path / "source"
    source.mkdir()
    source_env = _git_env(tmp_path / "source-env")
    _git(source, "init", "-q", env=source_env)
    _git(source, "config", "user.name", "Source Committer", env=source_env)
    _git(
        source,
        "config",
        "user.email",
        "source-committer@example.com",
        env=source_env,
    )
    result = _commit_file(
        source,
        _git_env_with_author_identity(
            source_env,
            name="Patch Author",
            email="patch-author@example.com",
        ),
        content="patch\n",
        filename="patch.txt",
        message_args=["-m", "Patch change"],
    )
    assert result.returncode == 0, result.stderr
    patch = tmp_path / "patch.diff"
    patch.write_text(
        _git(source, "format-patch", "-1", "--stdout", "HEAD", env=source_env).stdout,
        encoding="utf-8",
    )

    result = _git(repo, "am", str(patch), env=env, check=False)

    assert result.returncode == 0, result.stderr
    assert _head_author(repo, env) == "Patch Author <patch-author@example.com>"
    _assert_head_author_is_signed_off(repo, env)


def test_prepare_commit_msg_does_not_duplicate_existing_matching_signoff(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path)

    result = _commit_file(
        repo,
        env,
        content="first\n",
        message_args=[
            "-m",
            "Already signed",
            "-m",
            "Signed-off-by: Dev User <dev@example.com>",
        ],
    )

    assert result.returncode == 0, result.stderr
    assert _head_signoff_lines(repo, env).count(
        "Signed-off-by: Dev User <dev@example.com>"
    ) == 1


def test_prepare_commit_msg_adds_author_signoff_when_other_signoff_exists(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path)

    result = _commit_file(
        repo,
        env,
        content="first\n",
        message_args=[
            "-m",
            "Other signed",
            "-m",
            "Signed-off-by: Other Contributor <other@example.com>",
        ],
    )

    assert result.returncode == 0, result.stderr
    assert _head_signoff_lines(repo, env) == [
        "Signed-off-by: Other Contributor <other@example.com>",
        "Signed-off-by: Dev User <dev@example.com>",
    ]


def test_prepare_commit_msg_fails_loud_when_author_identity_is_unresolved(
    tmp_path: Path,
) -> None:
    repo, env = _init_repo(tmp_path, configure_identity=False)
    message_file = tmp_path / "message.txt"
    message_file.write_text("Unsigned\n", encoding="utf-8")

    result = subprocess.run(
        [str(HOOK_PATH), str(message_file)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "DCO sign-off requires a git author name and email" in result.stderr


def test_dco_message_hooks_are_tracked_executable_under_managed_hooks_path() -> None:
    assert HOOK_PATH.parent == REPO_ROOT / ".githooks"
    assert APPLYPATCH_HOOK_PATH.parent == REPO_ROOT / ".githooks"
    for hook_path in (HOOK_PATH, APPLYPATCH_HOOK_PATH):
        assert hook_path.exists()
        assert os.access(hook_path, os.X_OK)
