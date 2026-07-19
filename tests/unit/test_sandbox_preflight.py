"""Unit tests for the fail-closed sandbox launch guard (ADR-0034).

The ``--settings`` adapter can only lock the DENY floor; Claude merges array
settings across scopes, so a merged ``permissions.allow`` of a native write tool
or any ``sandbox.excludedCommands`` escapes the write/exec bound. These tests
pin the guard's decision matrix, the scope reader, and the fail-closed provider
launch path the reviewer asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.sandbox_scope import SandboxScope
from issue_orchestrator.execution.agent_runner_providers.claude import (
    ClaudeCodeProvider,
)
from issue_orchestrator.execution.agent_runner_providers.sandbox_preflight import (
    AmbientClaudeSettings,
    SandboxEnvironmentUnsafeError,
    assert_claude_sandbox_environment_safe,
    evaluate_sandbox_environment,
    read_ambient_claude_settings,
)


def _scope(worktree: Path = Path("/wt/issue-42")) -> SandboxScope:
    return SandboxScope(
        read_roots=(worktree,),
        write_roots=(worktree,),
        egress="model-only",
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )


def _ambient(
    *,
    allow: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
    locked: bool = False,
) -> AmbientClaudeSettings:
    return AmbientClaudeSettings(
        permission_allow=allow,
        excluded_commands=excluded,
        managed_permission_rules_locked=locked,
    )


# ---------------------------------------------------------------------------
# evaluate_sandbox_environment — pure decision matrix
# ---------------------------------------------------------------------------


def test_clean_environment_passes() -> None:
    evaluate_sandbox_environment(_scope(), _ambient())


def test_narrow_bash_allow_is_not_an_escape() -> None:
    # A specific sandboxed Bash allow does not defeat the OS filesystem bound.
    evaluate_sandbox_environment(_scope(), _ambient(allow=("Bash(npm run test)",)))


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_bare_native_write_allow_fails_closed(tool: str) -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="native writes"):
        evaluate_sandbox_environment(_scope(), _ambient(allow=(tool,)))


def test_excluded_commands_fail_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="excludedCommands"):
        evaluate_sandbox_environment(_scope(), _ambient(excluded=("curl",)))


def test_managed_lock_neutralizes_ambient_allow() -> None:
    # allowManagedPermissionRulesOnly makes Claude ignore ambient allow rules.
    evaluate_sandbox_environment(_scope(), _ambient(allow=("Edit", "Write"), locked=True))


def test_managed_lock_does_not_excuse_excluded_commands() -> None:
    # excludedCommands has no managed-only lock -> still prohibited.
    with pytest.raises(SandboxEnvironmentUnsafeError, match="excludedCommands"):
        evaluate_sandbox_environment(
            _scope(), _ambient(excluded=("docker",), locked=True)
        )


def test_write_allow_confined_to_write_root_is_allowed() -> None:
    # A native-write allow scoped to the session's write root is not an escape.
    worktree = Path("/wt/issue-9")
    entry = "Edit(//wt/issue-9/**)"  # // absolute specifier normalized to /wt/...
    evaluate_sandbox_environment(_scope(worktree), _ambient(allow=(entry,)))


def test_write_allow_to_other_path_fails_closed() -> None:
    worktree = Path("/wt/issue-9")
    with pytest.raises(SandboxEnvironmentUnsafeError):
        evaluate_sandbox_environment(
            _scope(worktree), _ambient(allow=("Edit(//etc/**)",))
        )


def test_error_carries_reasons() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError) as excinfo:
        evaluate_sandbox_environment(
            _scope(), _ambient(allow=("Edit",), excluded=("curl",))
        )
    assert len(excinfo.value.reasons) == 2


# ---------------------------------------------------------------------------
# read_ambient_claude_settings — scope merging (I/O)
# ---------------------------------------------------------------------------


def _write_settings(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_reader_unions_allow_across_user_and_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"allow": ["Read"]}})
    _write_settings(
        project / ".claude" / "settings.json", {"permissions": {"allow": ["Edit"]}}
    )
    ambient = read_ambient_claude_settings(
        home=home, project_dir=project, managed_settings_paths=()
    )
    assert set(ambient.permission_allow) == {"Read", "Edit"}


def test_reader_picks_up_local_excluded_commands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.local.json",
        {"sandbox": {"excludedCommands": ["docker *"]}},
    )
    ambient = read_ambient_claude_settings(
        home=tmp_path / "empty", project_dir=project, managed_settings_paths=()
    )
    assert ambient.excluded_commands == ("docker *",)


def test_reader_honors_config_dir_override(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _write_settings(config_dir / "settings.json", {"permissions": {"allow": ["Write"]}})
    ambient = read_ambient_claude_settings(
        home=tmp_path / "home",
        project_dir=tmp_path / "proj",
        config_dir=str(config_dir),
        managed_settings_paths=(),
    )
    assert ambient.permission_allow == ("Write",)


def test_reader_reads_managed_lock_only_from_managed_scope(tmp_path: Path) -> None:
    managed = tmp_path / "managed.json"
    _write_settings(managed, {"allowManagedPermissionRulesOnly": True})
    # A project file claiming the lock must NOT flip it (only managed scope can).
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.json",
        {"allowManagedPermissionRulesOnly": True},
    )
    from_managed = read_ambient_claude_settings(
        home=tmp_path / "h", project_dir=project, managed_settings_paths=(managed,)
    )
    assert from_managed.managed_permission_rules_locked is True
    without_managed = read_ambient_claude_settings(
        home=tmp_path / "h", project_dir=project, managed_settings_paths=()
    )
    assert without_managed.managed_permission_rules_locked is False


def test_reader_tolerates_missing_and_malformed_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    ambient = read_ambient_claude_settings(
        home=tmp_path / "nope", project_dir=project, managed_settings_paths=()
    )
    assert ambient == AmbientClaudeSettings((), (), False)


# ---------------------------------------------------------------------------
# assert_claude_sandbox_environment_safe + provider launch path (adversarial)
# ---------------------------------------------------------------------------


def test_provider_launch_fails_closed_on_widening_project_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's adversarial case: a target-repo .claude/settings.json that
    # grants Edit/Write and excludes a command. The opted-in launch must refuse.
    worktree = tmp_path / "worktree"
    _write_settings(
        worktree / ".claude" / "settings.json",
        {
            "permissions": {"allow": ["Edit", "Write"]},
            "sandbox": {"excludedCommands": ["curl"]},
        },
    )
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    monkeypatch.setenv("HOME", str(clean_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    scope = _scope(worktree)
    with pytest.raises(SandboxEnvironmentUnsafeError):
        # Managed paths default to non-existent system files on the test host.
        ClaudeCodeProvider().build_command(
            prompt="task", model="sonnet", sandbox_scope=scope
        )


def test_provider_launch_succeeds_on_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    monkeypatch.setenv("HOME", str(clean_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    cmd = ClaudeCodeProvider().build_command(
        prompt="task", model="sonnet", sandbox_scope=_scope(worktree)
    )
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"


def test_assert_helper_derives_project_dir_from_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    _write_settings(
        worktree / ".claude" / "settings.json", {"permissions": {"allow": ["Write"]}}
    )
    monkeypatch.setenv("HOME", str(tmp_path / "clean"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(SandboxEnvironmentUnsafeError):
        assert_claude_sandbox_environment_safe(_scope(worktree), managed_settings_paths=())
