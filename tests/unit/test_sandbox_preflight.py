"""Unit tests for the managed-lockdown launch guard (ADR-0034).

The guard no longer parses ambient settings (a build-time subset parser can be
neither authoritative nor durable — sources merge from unreadable locations and
hot-reload). It requires an installed managed lockdown that neutralizes ambient
widening and refuses to launch without it, validating the managed policy is
itself clean.
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
    ManagedLockdown,
    SandboxEnvironmentUnsafeError,
    assert_claude_sandbox_environment_safe,
    evaluate_managed_lockdown,
    read_managed_lockdown,
)

_HOME = Path("/home/agent")
_WORKTREE = Path("/wt/issue-9")


def _scope(worktree: Path = _WORKTREE, egress: str = "model-only") -> SandboxScope:
    return SandboxScope(
        read_roots=(worktree,),
        write_roots=(worktree,),
        egress=egress,  # type: ignore[arg-type]
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


_COMPLIANT = {
    "allowManagedPermissionRulesOnly": True,
    "allowManagedDomainsOnly": True,
    "permissions": {"deny": ["Read(~/.ssh/**)"]},
}


def _read(scope: SandboxScope, *paths: Path, dirs: tuple[Path, ...] = ()) -> ManagedLockdown:
    return read_managed_lockdown(
        scope,
        home=_HOME,
        project_dir=scope.write_roots[0],
        managed_settings_paths=paths,
        managed_settings_dirs=dirs,
    )


# ---------------------------------------------------------------------------
# evaluate_managed_lockdown — the required-lock decision
# ---------------------------------------------------------------------------


def test_no_managed_policy_fails_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="no managed settings"):
        evaluate_managed_lockdown(
            _scope(), ManagedLockdown(present=False, permission_rules_locked=False, domains_locked=False)
        )


def test_missing_permission_lock_fails_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowManagedPermissionRulesOnly"):
        evaluate_managed_lockdown(
            _scope(), ManagedLockdown(present=True, permission_rules_locked=False, domains_locked=True)
        )


def test_missing_domain_lock_fails_closed_for_restricted_egress() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="allowManagedDomainsOnly"):
        evaluate_managed_lockdown(
            _scope(egress="model-only"),
            ManagedLockdown(present=True, permission_rules_locked=True, domains_locked=False),
        )


def test_model_web_egress_does_not_require_domain_lock() -> None:
    evaluate_managed_lockdown(
        _scope(egress="model+web"),
        ManagedLockdown(present=True, permission_rules_locked=True, domains_locked=False),
    )


def test_full_lockdown_passes() -> None:
    evaluate_managed_lockdown(
        _scope(), ManagedLockdown(present=True, permission_rules_locked=True, domains_locked=True)
    )


def test_managed_widening_findings_fail_closed() -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError, match="native writes"):
        evaluate_managed_lockdown(
            _scope(),
            ManagedLockdown(
                present=True,
                permission_rules_locked=True,
                domains_locked=True,
                widening_findings=("managed permissions.allow native writes outside roots: ['Write']",),
            ),
        )


# ---------------------------------------------------------------------------
# read_managed_lockdown — parse the authoritative managed scope
# ---------------------------------------------------------------------------


def test_reads_locks_and_passes_clean_policy(tmp_path: Path) -> None:
    managed = tmp_path / "managed-settings.json"
    _write(managed, _COMPLIANT)
    lockdown = _read(_scope(), managed)
    assert lockdown.present and lockdown.permission_rules_locked and lockdown.domains_locked
    assert lockdown.widening_findings == ()


def test_domain_lock_accepted_at_nested_location(tmp_path: Path) -> None:
    managed = tmp_path / "managed-settings.json"
    _write(
        managed,
        {
            "allowManagedPermissionRulesOnly": True,
            "sandbox": {"network": {"allowManagedDomainsOnly": True}},
        },
    )
    assert _read(_scope(), managed).domains_locked is True


def test_managed_settings_d_dropins_merge(tmp_path: Path) -> None:
    base = tmp_path / "managed-settings.json"
    _write(base, {"allowManagedPermissionRulesOnly": True})
    dropin_dir = tmp_path / "managed-settings.d"
    _write(dropin_dir / "10-net.json", {"allowManagedDomainsOnly": True})
    lockdown = _read(_scope(), base, dirs=(dropin_dir,))
    assert lockdown.permission_rules_locked and lockdown.domains_locked


@pytest.mark.parametrize(
    "policy,needle",
    [
        ({"permissions": {"allow": ["Write"]}}, "native writes"),
        ({"permissions": {"allow": ["Edit(//wt/issue-90/**)"]}}, "native writes"),  # sibling
        ({"permissions": {"allow": ["Edit(//wt/issue-9*/**)"]}}, "native writes"),  # wildcard
        ({"sandbox": {"filesystem": {"allowWrite": ["/tmp/x"]}}}, "allowWrite"),
        ({"sandbox": {"network": {"allowedDomains": ["evil.example"]}}}, "allowedDomains"),
        ({"sandbox": {"network": {"allowAllUnixSockets": True}}}, "allowAllUnixSockets"),
        ({"sandbox": {"excludedCommands": ["curl"]}}, "excludedCommands"),
        ({"sandbox": {"allowUnixSockets": ["/s"]}}, "allowUnixSockets"),
        ({"sandbox": {"enableWeakerNestedSandbox": True}}, "enableWeakerNestedSandbox"),
    ],
)
def test_managed_policy_widening_is_flagged(tmp_path: Path, policy: dict, needle: str) -> None:
    managed = tmp_path / "managed-settings.json"
    _write(managed, {**_COMPLIANT, **policy})
    findings = _read(_scope(), managed).widening_findings
    assert any(needle in f for f in findings), findings


def test_managed_write_allow_confined_to_worktree_is_clean(tmp_path: Path) -> None:
    managed = tmp_path / "managed-settings.json"
    _write(managed, {**_COMPLIANT, "permissions": {"allow": ["Edit(//wt/issue-9/**)"]}})
    assert _read(_scope(), managed).widening_findings == ()


def test_missing_managed_files_are_absent(tmp_path: Path) -> None:
    lockdown = _read(_scope(), tmp_path / "nope.json")
    assert lockdown.present is False


# ---------------------------------------------------------------------------
# assert_claude_sandbox_environment_safe + provider launch path
# ---------------------------------------------------------------------------


def test_assert_passes_with_compliant_managed_policy(tmp_path: Path) -> None:
    managed = tmp_path / "managed-settings.json"
    _write(managed, _COMPLIANT)
    assert_claude_sandbox_environment_safe(
        _scope(), home=_HOME, managed_settings_paths=(managed,), managed_settings_dirs=()
    )


def test_assert_refuses_without_managed_policy(tmp_path: Path) -> None:
    with pytest.raises(SandboxEnvironmentUnsafeError):
        assert_claude_sandbox_environment_safe(
            _scope(),
            home=_HOME,
            managed_settings_paths=(tmp_path / "nope.json",),
            managed_settings_dirs=(),
        )


def test_provider_launch_fails_closed_without_managed_lockdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No managed policy at the default system paths on the test host -> refuse.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "clean"))
    with pytest.raises(SandboxEnvironmentUnsafeError):
        ClaudeCodeProvider().build_command(
            prompt="task", model="sonnet", sandbox_scope=_scope(worktree)
        )
