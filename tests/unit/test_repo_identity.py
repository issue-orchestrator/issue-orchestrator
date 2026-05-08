from pathlib import Path

from issue_orchestrator.infra.repo_identity import (
    RepoIdentity,
    build_repo_identity,
    build_repo_identity_with_status,
    deserialize_repo_identity,
    diff_repo_identity,
    get_repo_head_sha,
    serialize_repo_identity,
)


def test_get_repo_head_sha_from_git_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_dir = repo / ".git"
    git_dir.mkdir()

    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs = git_dir / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("abc123\n")

    assert get_repo_head_sha(repo) == "abc123"


def test_get_repo_head_sha_from_worktree_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree_git = tmp_path / "worktree_git"
    worktree_git.mkdir()

    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n")
    refs = worktree_git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "feature").write_text("def456\n")

    (repo / ".git").write_text(f"gitdir: {worktree_git}\n")

    assert get_repo_head_sha(repo) == "def456"


def test_get_repo_head_sha_from_linked_worktree_common_ref(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    common_git = tmp_path / "common_git"
    worktree_git = common_git / "worktrees" / "repo"
    worktree_git.mkdir(parents=True)

    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (worktree_git / "commondir").write_text("../..\n")
    refs = common_git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "feature").write_text("fedcba\n")

    (repo / ".git").write_text(f"gitdir: {worktree_git}\n")

    assert get_repo_head_sha(repo) == "fedcba"


def test_get_repo_head_sha_from_linked_worktree_absolute_common_ref(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    common_git = tmp_path / "common_git"
    worktree_git = tmp_path / "worktrees" / "repo"
    worktree_git.mkdir(parents=True)

    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (worktree_git / "commondir").write_text(f"{common_git}\n")
    refs = common_git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "feature").write_text("654321\n")

    (repo / ".git").write_text(f"gitdir: {worktree_git}\n")

    assert get_repo_head_sha(repo) == "654321"


def test_get_repo_head_sha_from_linked_worktree_common_packed_refs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    common_git = tmp_path / "common_git"
    worktree_git = common_git / "worktrees" / "repo"
    worktree_git.mkdir(parents=True)

    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (worktree_git / "commondir").write_text("../..\n")
    (common_git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "1234567890abcdef1234567890abcdef12345678 refs/heads/feature\n"
    )

    (repo / ".git").write_text(f"gitdir: {worktree_git}\n")

    assert get_repo_head_sha(repo) == "1234567890abcdef1234567890abcdef12345678"


def test_repo_identity_roundtrip_serialization():
    identity = RepoIdentity(
        repo_root="/tmp/repo",
        commit_sha="abc123",
        branch="main",
        working_tree_dirty=True,
        dirty_fingerprint="deadbeef",
        source_root="/tmp/repo/src",
    )

    serialized = serialize_repo_identity(identity)
    restored = deserialize_repo_identity(serialized)

    assert restored == identity


def test_diff_repo_identity_reports_mismatches():
    expected = RepoIdentity(
        repo_root="/tmp/repo",
        commit_sha="abc123",
        branch="main",
        working_tree_dirty=False,
        dirty_fingerprint=None,
        source_root="/tmp/repo/src",
    )
    observed = RepoIdentity(
        repo_root="/tmp/repo",
        commit_sha="def456",
        branch="feature",
        working_tree_dirty=True,
        dirty_fingerprint="f00dbabe",
        source_root="/tmp/repo/src",
    )

    mismatches = diff_repo_identity(expected, observed)

    assert mismatches["commit_sha"] == {"expected": "abc123", "observed": "def456"}
    assert mismatches["branch"] == {"expected": "main", "observed": "feature"}
    assert mismatches["working_tree_dirty"] == {"expected": False, "observed": True}
    assert mismatches["dirty_fingerprint"] == {"expected": None, "observed": "f00dbabe"}


def test_build_repo_identity_for_non_git_dir(tmp_path):
    identity = build_repo_identity(tmp_path)

    assert identity.repo_root == str(tmp_path.resolve())
    assert identity.commit_sha is None
    assert identity.working_tree_dirty is False


def test_build_repo_identity_with_status_resolver_sets_dirty_fingerprint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_dir = repo / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs = git_dir / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("abc123\n")

    def resolver(_: Path) -> tuple[str | None, list[str]]:
        return "feature/test", [" M src/file.py", "?? new.txt"]

    identity = build_repo_identity_with_status(repo, status_resolver=resolver)

    assert identity.branch == "feature/test"
    assert identity.working_tree_dirty is True
    assert identity.dirty_fingerprint is not None
    assert len(identity.dirty_fingerprint) == 16
