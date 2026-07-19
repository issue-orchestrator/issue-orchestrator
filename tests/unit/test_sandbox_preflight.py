"""Unit tests for the fail-closed sandbox launch guard (ADR-0034).

Covers the COMPLETE widening model: native-write allows, filesystem.allowWrite,
network.allowedDomains, excludedCommands, allowUnixSockets, and permission hooks;
provenance (validate the effective managed set when locked, do not skip); and
path-component confinement (sibling-prefix and wildcard-escape are rejected).
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
    ScopedEntries,
    assert_claude_sandbox_environment_safe,
    evaluate_sandbox_environment,
    read_ambient_claude_settings,
)

_HOME = Path("/home/agent")


def _scope(worktree: Path = Path("/wt/issue-9"), egress: str = "model-only") -> SandboxScope:
    return SandboxScope(
        read_roots=(worktree,),
        write_roots=(worktree,),
        egress=egress,  # type: ignore[arg-type]
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )


def _ambient(
    *,
    allow_m: tuple[str, ...] = (),
    allow_n: tuple[str, ...] = (),
    write_m: tuple[str, ...] = (),
    write_n: tuple[str, ...] = (),
    dom_m: tuple[str, ...] = (),
    dom_n: tuple[str, ...] = (),
    excl_m: tuple[str, ...] = (),
    excl_n: tuple[str, ...] = (),
    sock_m: tuple[str, ...] = (),
    sock_n: tuple[str, ...] = (),
    hooks_m: tuple[str, ...] = (),
    hooks_n: tuple[str, ...] = (),
    perm_locked: bool = False,
    dom_locked: bool = False,
) -> AmbientClaudeSettings:
    return AmbientClaudeSettings(
        permission_allow=ScopedEntries(allow_m, allow_n),
        filesystem_allow_write=ScopedEntries(write_m, write_n),
        network_allowed_domains=ScopedEntries(dom_m, dom_n),
        excluded_commands=ScopedEntries(excl_m, excl_n),
        allow_unix_sockets=ScopedEntries(sock_m, sock_n),
        permission_hooks=ScopedEntries(hooks_m, hooks_n),
        managed_permission_rules_locked=perm_locked,
        managed_domains_locked=dom_locked,
    )


def _eval(scope: SandboxScope, ambient: AmbientClaudeSettings) -> None:
    evaluate_sandbox_environment(
        scope, ambient, home=_HOME, project_dir=scope.write_roots[0]
    )


# ---------------------------------------------------------------------------
# Clean + native-write allows (with fixed confinement)
# ---------------------------------------------------------------------------


def test_clean_environment_passes() -> None:
    _eval(_scope(), _ambient())


def test_narrow_bash_allow_is_not_an_escape() -> None:
    _eval(_scope(), _ambient(allow_n=("Bash(npm run test)",)))


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_bare_native_write_allow_fails_closed(tool: str) -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="native writes"):
        _eval(_scope(), _ambient(allow_n=(tool,)))


def test_write_allow_confined_to_write_root_passes() -> None:
    _eval(_scope(), _ambient(allow_n=("Edit(//wt/issue-9/**)",)))


def test_write_allow_sibling_prefix_fails_closed() -> None:
    # /wt/issue-90 is a SIBLING of /wt/issue-9, not a descendant.
    with pytest.raises(SandboxEnvironmentUnsafeError):
        _eval(_scope(), _ambient(allow_n=("Edit(//wt/issue-90/**)",)))


def test_write_allow_wildcard_escape_fails_closed() -> None:
    # //wt/issue-9*/** is confined only by its non-glob parent /wt (outside root).
    with pytest.raises(SandboxEnvironmentUnsafeError):
        _eval(_scope(), _ambient(allow_n=("Edit(//wt/issue-9*/**)",)))


# ---------------------------------------------------------------------------
# filesystem.allowWrite  (P0: was not read at all)
# ---------------------------------------------------------------------------


def test_allow_write_outside_worktree_fails_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowWrite"):
        _eval(_scope(), _ambient(write_n=("/tmp/outside-worktree",)))


def test_allow_write_home_relative_fails_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowWrite"):
        _eval(_scope(), _ambient(write_n=("~/.kube",)))


def test_allow_write_project_relative_inside_worktree_passes() -> None:
    _eval(_scope(), _ambient(write_n=("./build", "output")))


def test_managed_allow_write_outside_still_fails_closed() -> None:
    # allowWrite has no managed-only lock; a managed entry still widens.
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowWrite"):
        _eval(_scope(), _ambient(write_m=("/srv/data",)))


# ---------------------------------------------------------------------------
# network.allowedDomains  (P0: was not read at all)
# ---------------------------------------------------------------------------


def test_allowed_domains_widening_fails_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowedDomains"):
        _eval(_scope(egress="model-only"), _ambient(dom_n=("example.com",)))


def test_model_api_domain_is_permitted_under_model_only() -> None:
    _eval(_scope(egress="model-only"), _ambient(dom_n=("api.anthropic.com",)))


def test_none_egress_permits_no_domain() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowedDomains"):
        _eval(_scope(egress="none"), _ambient(dom_n=("api.anthropic.com",)))


def test_model_web_egress_skips_domain_check() -> None:
    _eval(_scope(egress="model+web"), _ambient(dom_n=("example.com", "evil.net")))


def test_managed_domains_lock_ignores_nonmanaged_but_validates_managed() -> None:
    # Locked -> non-managed domains ignored (pass) ...
    _eval(_scope(), _ambient(dom_n=("example.com",), dom_locked=True))
    # ... but a managed domain is the effective set and is still validated.
    with pytest.raises(SandboxEnvironmentUnsafeError):
        _eval(_scope(), _ambient(dom_m=("example.com",), dom_locked=True))


# ---------------------------------------------------------------------------
# excludedCommands / allowUnixSockets / hooks
# ---------------------------------------------------------------------------


def test_excluded_commands_fail_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="excludedCommands"):
        _eval(_scope(), _ambient(excl_n=("curl",)))


def test_allow_unix_sockets_fail_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowUnixSockets"):
        _eval(_scope(), _ambient(sock_n=("/var/run/docker.sock",)))


def test_permission_hooks_fail_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="hook"):
        _eval(_scope(), _ambient(hooks_n=("PreToolUse",)))


# ---------------------------------------------------------------------------
# Provenance (P0: validate the effective set, do not skip when locked)
# ---------------------------------------------------------------------------


def test_perm_lock_ignores_nonmanaged_allow() -> None:
    # allowManagedPermissionRulesOnly makes Claude ignore non-managed allows.
    _eval(_scope(), _ambient(allow_n=("Edit", "Write"), perm_locked=True))


def test_perm_lock_still_validates_managed_allow() -> None:
    # The reviewer's case: managed lock + managed Write is still a host grant.
    with pytest.raises(SandboxEnvironmentUnsafeError, match="native writes"):
        _eval(_scope(), _ambient(allow_m=("Write",), perm_locked=True))


def test_unlocked_validates_the_union() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError):
        _eval(_scope(), _ambient(allow_n=("Write",), perm_locked=False))


def test_error_carries_all_reasons() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError) as excinfo:
        _eval(
            _scope(),
            _ambient(allow_n=("Edit",), write_n=("/etc",), excl_n=("curl",)),
        )
    assert len(excinfo.value.reasons) == 3


# ---------------------------------------------------------------------------
# read_ambient_claude_settings — scope merging + provenance (I/O)
# ---------------------------------------------------------------------------


def _write_settings(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_reader_splits_managed_and_nonmanaged_provenance(tmp_path: Path) -> None:
    managed = tmp_path / "managed.json"
    _write_settings(managed, {"permissions": {"allow": ["Write"]}})
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.json", {"permissions": {"allow": ["Edit"]}}
    )
    ambient = read_ambient_claude_settings(
        home=tmp_path / "home",
        project_dir=project,
        managed_settings_paths=(managed,),
    )
    assert ambient.permission_allow.managed == ("Write",)
    assert ambient.permission_allow.nonmanaged == ("Edit",)


def test_reader_collects_all_widening_fields(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.json",
        {
            "sandbox": {
                "filesystem": {"allowWrite": ["/tmp/x"]},
                "network": {"allowedDomains": ["evil.com"], "allowUnixSockets": ["/s"]},
                "excludedCommands": ["docker *"],
            },
            "hooks": {"PreToolUse": [{"matcher": "*"}]},
        },
    )
    ambient = read_ambient_claude_settings(
        home=tmp_path / "h", project_dir=project, managed_settings_paths=()
    )
    assert ambient.filesystem_allow_write.nonmanaged == ("/tmp/x",)
    assert ambient.network_allowed_domains.nonmanaged == ("evil.com",)
    assert ambient.allow_unix_sockets.nonmanaged == ("/s",)
    assert ambient.excluded_commands.nonmanaged == ("docker *",)
    assert ambient.permission_hooks.nonmanaged == ("PreToolUse",)


def test_reader_honors_config_dir_override(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _write_settings(config_dir / "settings.json", {"permissions": {"allow": ["Write"]}})
    ambient = read_ambient_claude_settings(
        home=tmp_path / "home",
        project_dir=tmp_path / "proj",
        config_dir=str(config_dir),
        managed_settings_paths=(),
    )
    assert ambient.permission_allow.nonmanaged == ("Write",)


def test_reader_reads_locks_only_from_managed_scope(tmp_path: Path) -> None:
    managed = tmp_path / "managed.json"
    _write_settings(
        managed,
        {"allowManagedPermissionRulesOnly": True, "allowManagedDomainsOnly": True},
    )
    project = tmp_path / "project"
    _write_settings(
        project / ".claude" / "settings.json",
        {"allowManagedPermissionRulesOnly": True},  # must NOT flip the lock
    )
    with_managed = read_ambient_claude_settings(
        home=tmp_path / "h", project_dir=project, managed_settings_paths=(managed,)
    )
    assert with_managed.managed_permission_rules_locked is True
    assert with_managed.managed_domains_locked is True
    without = read_ambient_claude_settings(
        home=tmp_path / "h", project_dir=project, managed_settings_paths=()
    )
    assert without.managed_permission_rules_locked is False


def test_reader_tolerates_missing_and_malformed_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    ambient = read_ambient_claude_settings(
        home=tmp_path / "nope", project_dir=project, managed_settings_paths=()
    )
    assert ambient.permission_allow.union() == ()
    assert ambient.filesystem_allow_write.union() == ()


# ---------------------------------------------------------------------------
# Provider launch path (adversarial, both new fields)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"permissions": {"allow": ["Edit", "Write"]}, "sandbox": {"excludedCommands": ["curl"]}},
        {"sandbox": {"filesystem": {"allowWrite": ["/tmp/outside-worktree"]}}},
        {"sandbox": {"network": {"allowedDomains": ["example.com"]}}},
    ],
)
def test_provider_launch_fails_closed_on_widening_project_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    worktree = tmp_path / "worktree"
    _write_settings(worktree / ".claude" / "settings.json", payload)
    monkeypatch.setenv("HOME", str(tmp_path / "clean-home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(SandboxEnvironmentUnsafeError):
        ClaudeCodeProvider().build_command(
            prompt="task", model="sonnet", sandbox_scope=_scope(worktree)
        )


def test_provider_launch_succeeds_on_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "clean-home"))
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
        worktree / ".claude" / "settings.json",
        {"sandbox": {"filesystem": {"allowWrite": ["/etc"]}}},
    )
    monkeypatch.setenv("HOME", str(tmp_path / "clean"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(SandboxEnvironmentUnsafeError):
        assert_claude_sandbox_environment_safe(_scope(worktree), managed_settings_paths=())
