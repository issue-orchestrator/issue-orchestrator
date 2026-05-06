"""Unit tests for ``infra.static_version``.

The cache-buster token + sidebar SHA both flow through this module. We
test the resolver directly so a regression here surfaces before the
broader rendered-template tests.
"""

from __future__ import annotations

import os
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


def test_resolve_source_repo_root_finds_source_checkout(tmp_path: Path):
    """A real source checkout (with our ``__init__.py``) is accepted."""
    repo_root = tmp_path / "fake-repo"
    pkg_dir = repo_root / "src" / "issue_orchestrator"
    pkg_dir.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    init_file = pkg_dir / "__init__.py"
    init_file.write_text("# fake package")

    found = static_version._resolve_source_repo_root(
        pkg_dir,
        package_init=init_file,
    )

    assert found is not None
    assert found.resolve() == repo_root.resolve()


def test_resolve_source_repo_root_returns_none_for_unrelated_parent_repo(
    tmp_path: Path,
):
    """Wheel install under a target repo's ``.venv`` must NOT leak the target SHA.

    Reviewer concern on PR #6266: a wheel installed at
    ``<target-repo>/.venv/lib/.../site-packages/issue_orchestrator``
    must not cause the walker to land on ``<target-repo>`` and report
    an unrelated SHA. The samefile check on the running ``__init__.py``
    rules that out — we set up two distinct ``__init__.py`` files in
    this test, simulating the wheel install + the actual running
    package living elsewhere.
    """
    target_repo = tmp_path / "unrelated-target-repo"
    (target_repo / ".git").mkdir(parents=True)
    site_packages_pkg = (
        target_repo
        / ".venv"
        / "lib"
        / "python3.14"
        / "site-packages"
        / "issue_orchestrator"
    )
    site_packages_pkg.mkdir(parents=True)
    wheel_init = site_packages_pkg / "__init__.py"
    wheel_init.write_text("# pretend wheel-installed package")

    # Pretend the *real* running package lives somewhere else entirely
    # — a different file path that won't samefile() against any
    # ``__init__.py`` under ``target_repo``.
    elsewhere = tmp_path / "actual-running-package" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("# running package")

    found = static_version._resolve_source_repo_root(
        site_packages_pkg,
        package_init=elsewhere,
    )

    assert found is None, (
        "Walker must not return an unrelated parent .git when the "
        "source identity check fails; otherwise the cc sidebar would "
        "report the target repo's SHA."
    )


def test_resolve_source_repo_root_recognizes_flat_layout(tmp_path: Path):
    """Flat layout (no ``src/``) is also accepted when the package init matches."""
    repo_root = tmp_path / "flat-layout-repo"
    pkg_dir = repo_root / "issue_orchestrator"
    pkg_dir.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    init_file = pkg_dir / "__init__.py"
    init_file.write_text("# flat-layout package")

    found = static_version._resolve_source_repo_root(
        pkg_dir,
        package_init=init_file,
    )

    assert found is not None
    assert found.resolve() == repo_root.resolve()


def test_resolve_source_repo_root_returns_none_outside_any_repo(tmp_path: Path):
    """No ``.git`` anywhere on the path → ``None`` (not a crash)."""
    deep = tmp_path / "no" / "git" / "anywhere"
    deep.mkdir(parents=True)
    fake_init = deep / "__init__.py"
    fake_init.write_text("# isolated")

    assert (
        static_version._resolve_source_repo_root(deep, package_init=fake_init)
        is None
    )


def test_resolve_source_repo_root_follows_symlinks(tmp_path: Path):
    """Editable installs sometimes go through symlinks; samefile must resolve them."""
    real_repo = tmp_path / "real-repo"
    real_pkg = real_repo / "src" / "issue_orchestrator"
    real_pkg.mkdir(parents=True)
    (real_repo / ".git").mkdir()
    real_init = real_pkg / "__init__.py"
    real_init.write_text("# real package")

    link_root = tmp_path / "linked-repo"
    link_root.mkdir()
    # Symlink the entire repo dir; the walker should still resolve to
    # the real directory and accept it.
    link_path = link_root / "linked"
    os.symlink(real_repo, link_path)
    linked_pkg = link_path / "src" / "issue_orchestrator"
    linked_init = linked_pkg / "__init__.py"

    found = static_version._resolve_source_repo_root(
        linked_pkg,
        package_init=linked_init,
    )

    assert found is not None
    assert found.resolve() == real_repo.resolve()
