import os
from pathlib import Path

from issue_orchestrator.infra.worktree_base import resolve_base_branch


def test_resolve_base_branch_prefers_config_override():
    called = {"count": 0}

    def resolver(_: Path) -> str:
        called["count"] += 1
        return "main"

    result = resolve_base_branch(
        Path("/repo"),
        config_override="release",
        default_branch_resolver=resolver,
        env_override="env-branch",
    )

    assert result.branch == "release"
    assert result.source == "config_override"
    assert called["count"] == 0


def test_resolve_base_branch_uses_env_override():
    called = {"count": 0}

    def resolver(_: Path) -> str:
        called["count"] += 1
        return "main"

    result = resolve_base_branch(
        Path("/repo"),
        config_override=None,
        default_branch_resolver=resolver,
        env_override="env-branch",
    )

    assert result.branch == "env-branch"
    assert result.source == "env_override"
    assert called["count"] == 0


def test_resolve_base_branch_falls_back_to_auto_detect():
    called = {"count": 0}

    def resolver(_: Path) -> str:
        called["count"] += 1
        return "main"

    # Temporarily remove the env var if set
    old_env = os.environ.pop("ORCHESTRATOR_WORKTREE_BASE_BRANCH", None)
    try:
        result = resolve_base_branch(
            Path("/repo"),
            config_override=None,
            default_branch_resolver=resolver,
            env_override=None,
        )

        assert result.branch == "main"
        assert result.source == "auto_detect"
        assert called["count"] == 1
    finally:
        if old_env is not None:
            os.environ["ORCHESTRATOR_WORKTREE_BASE_BRANCH"] = old_env
