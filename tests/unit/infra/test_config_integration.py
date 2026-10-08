"""The ``integration:`` config section (#8144): parse, implication, YAML, settings."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import IntegrationConfig
from issue_orchestrator.infra.settings_schema import (
    IntegrationSettings,
    apply_to,
    build_save_plan,
    from_config,
)


def _load(tmp_path: Path, body: str) -> Config:
    path = tmp_path / ".issue-orchestrator/config/modes/default/default.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("repo:\n  name: owner/repo\n" + body)
    return Config.load(path)


def test_defaults_are_off_and_unserialized(tmp_path: Path) -> None:
    config = _load(tmp_path, "")
    assert config.integration == IntegrationConfig()
    assert config.integration.enabled is False
    assert config.worktree_base_branch_override is None
    assert "integration" not in config.to_dict()


def test_every_field_parses(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        "integration:\n  enabled: true\n  branch: staging\n  deliver: manual\n"
        "  merge_method: squash\n  merge_after: tech-lead-reviewed\n",
    )
    assert config.integration == IntegrationConfig(
        enabled=True, branch="staging", deliver="manual",
        merge_method="squash", merge_after="tech-lead-reviewed",
    )


@pytest.mark.parametrize(
    "body, match",
    [
        ("  deliver: cadence\n", "integration.deliver"),
        ("  deliver: milestone\n", "integration.deliver"),
        ("  merge_method: octopus\n", "integration.merge_method"),
        ("  merge_after: approved\n", "integration.merge_after"),
        ("  branch: origin/integration\n", "integration.branch"),
        ("  branch: ''\n", "integration.branch"),
        ("  enabled: 'yes'\n", "integration.enabled"),
        ("  brnach: integration\n", "unknown keys"),
    ],
)
def test_invalid_values_fail_at_load(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, "integration:\n" + body)


def test_enabled_implies_the_worktree_base_override(tmp_path: Path) -> None:
    config = _load(tmp_path, "integration:\n  enabled: true\n  branch: staging\n")
    assert config.worktree_base_branch_override == "staging"


def test_disabled_section_implies_nothing(tmp_path: Path) -> None:
    config = _load(tmp_path, "integration:\n  enabled: false\n  branch: staging\n")
    assert config.worktree_base_branch_override is None


def test_equal_explicit_override_is_accepted(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        "worktrees:\n  base_branch_override: staging\n"
        "integration:\n  enabled: true\n  branch: staging\n",
    )
    assert config.worktree_base_branch_override == "staging"


def test_conflicting_override_fails_naming_both(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'develop'.*'staging'"):
        _load(
            tmp_path,
            "worktrees:\n  base_branch_override: develop\n"
            "integration:\n  enabled: true\n  branch: staging\n",
        )


def test_merge_queue_and_integration_cannot_both_be_enabled(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="merge_queue.enabled"):
        _load(tmp_path, "merge_queue:\n  enabled: true\nintegration:\n  enabled: true\n")


def test_yaml_round_trip_does_not_pin_the_implied_override(tmp_path: Path) -> None:
    config = _load(tmp_path, "integration:\n  enabled: true\n  branch: staging\n  merge_method: rebase\n")
    serialized = config.to_dict()
    assert serialized["integration"] == {"enabled": True, "branch": "staging", "merge_method": "rebase"}
    assert "base_branch_override" not in serialized.get("worktrees", {})
    # Changing the branch after a round trip must not trip the conflict check.
    serialized["integration"]["branch"] = "next"
    path = tmp_path / ".issue-orchestrator/config/modes/default/default.yaml"
    path.write_text(yaml.safe_dump(serialized))
    reloaded = Config.load(path)
    assert reloaded.integration.branch == "next"
    assert reloaded.worktree_base_branch_override == "next"


def test_a_real_override_still_serializes_when_integration_is_off(tmp_path: Path) -> None:
    config = _load(tmp_path, "worktrees:\n  base_branch_override: develop\n")
    assert config.to_dict()["worktrees"]["base_branch_override"] == "develop"


def test_event_dict_carries_the_section() -> None:
    assert Config().to_event_dict()["integration"]["enabled"] is False


def test_settings_tab_round_trips_and_requires_restart() -> None:
    config = Config()
    tabs = from_config(config)
    assert isinstance(tabs["integration"], IntegrationSettings)
    tabs["integration"] = IntegrationSettings(
        enabled=True, branch="staging", deliver="manual", merge_method="squash",
        merge_after="tech-lead-reviewed",
    )
    target = Config()
    assert apply_to(tabs, target) is True
    assert target.integration.enabled is True
    assert target.integration.branch == "staging"
    assert target.integration.merge_method == "squash"
    assert target.integration.merge_after == "tech-lead-reviewed"
    paths = {entry.yaml_path for entry in build_save_plan(from_config(config), tabs).entries}
    assert {"integration.enabled", "integration.branch", "integration.merge_method",
            "integration.merge_after"} <= paths


@pytest.mark.parametrize(
    "field, value", [("deliver", "cadence"), ("branch", "origin/x"), ("merge_method", "octopus")]
)
def test_settings_tab_rejects_unsupported_values(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        IntegrationSettings(**{field: value})
