from issue_orchestrator.infra.repo_identity import get_repo_head_sha


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
