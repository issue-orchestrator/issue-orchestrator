"""Unit tests for ``infra.static_version``.

The cache-buster token + sidebar SHA both flow through this module. We
test the resolver directly so a regression here surfaces before the
broader rendered-template tests.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.infra import static_version


def test_resolve_cc_commit_sha_returns_string_or_none():
    """Resolver returns a 40-char hex SHA when running from the source tree.

    Under the test runner the worktree's ``.git`` directory is
    discoverable from the package install path, so this should produce
    a real SHA. We assert shape rather than an exact value to avoid
    coupling the test to the tip of main.
    """
    sha = static_version.resolve_cc_commit_sha()

    if sha is None:
        # Acceptable for non-source installs (wheel-only environments).
        return
    assert isinstance(sha, str)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_static_version_token_is_non_empty_and_short():
    """Token must be non-empty and bounded so it does not bloat URLs."""
    token = static_version.STATIC_VERSION_TOKEN

    assert token
    assert isinstance(token, str)
    # Commit SHA path → 12 chars; fallback ``start-<epoch>`` is also short.
    assert len(token) <= 32
    assert "{{" not in token
    assert "/" not in token


def test_walk_up_for_git_dir_finds_repo_root_from_package_path(tmp_path: Path):
    """The walk-up helper must find ``.git`` somewhere on the way to ``/``.

    Synthetic fixture: creates a fake ``.git`` directory and a nested
    leaf, then asserts the walk-up resolves to the directory holding
    ``.git`` regardless of starting depth.
    """
    repo_root = tmp_path / "fake-repo"
    (repo_root / "a" / "b" / "c").mkdir(parents=True)
    (repo_root / ".git").mkdir()

    found = static_version._walk_up_for_git_dir(repo_root / "a" / "b" / "c")

    assert found is not None
    assert found.resolve() == repo_root.resolve()


def test_walk_up_for_git_dir_returns_none_outside_any_repo(tmp_path: Path):
    """No ``.git`` anywhere on the path → ``None`` (not a crash)."""
    deep = tmp_path / "no" / "git" / "anywhere"
    deep.mkdir(parents=True)

    assert static_version._walk_up_for_git_dir(deep) is None
