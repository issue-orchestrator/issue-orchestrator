"""Tests to verify skill sharing symlinks stay intact."""

from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_codex_skills_symlinks_to_claude_skills() -> None:
    """Verify .codex/skills is a symlink to .claude/skills."""
    repo = _repo_root()
    codex_skills = repo / ".codex" / "skills"
    claude_skills = repo / ".claude" / "skills"

    assert codex_skills.exists(), ".codex/skills does not exist"
    assert codex_skills.is_symlink(), ".codex/skills is not a symlink"
    assert codex_skills.resolve() == claude_skills.resolve(), (
        f".codex/skills points to {codex_skills.resolve()}, "
        f"expected {claude_skills.resolve()}"
    )
