"""A restart onto new code restores the live sessions of an unchanged config.

The configuration fingerprints used to hash the loaded ``Config`` dataclass in
full, so every field the code added and every default it changed moved the
fingerprint of an UNCHANGED operator YAML - and ``SessionRestorer`` then
refused to restore any live session across the upgrade, stopping startup.
They now hash what the operator configured; a restored session is bound only
to the settings a running engine does not apply live.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

from issue_orchestrator.control.session_restorer import (
    SessionConfigurationModeMismatchError,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.session_runner import DiscoveredSession
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.test_session_restorer import (
    RUN_LEDGER,
    MockRepositoryHost,
    MockWorkingCopy,
    restorer_for,
)

_YAML = """\
repo:
  name: owner/repo
agents:
  agent:web:
    prompt: prompt.md
    model: sonnet
tech_lead:
  max_expedited: 3
"""


def _config_path(tmp_path: Path, text: str = _YAML) -> Path:
    path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    (tmp_path / "prompt.md").write_text("Fix it", encoding="utf-8")
    return path


def _pre_upgrade_fingerprint(config: Config) -> str:
    """The stamp code before this change wrote: the whole dataclass, hashed."""
    snapshot = asdict(config)
    for key in (
        "launch_selection", "config_fingerprint", "config_path",
        "session_binding_fingerprint", "loaded_effective_state",
    ):
        snapshot.pop(key, None)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _launch_stamp(config: Config) -> dict[str, object]:
    """What the launcher records for a session it starts under ``config``."""
    return {
        "configuration_mode": config.configuration_mode,
        "config_name": config.config_name,
        "config_fingerprint": config.config_fingerprint,
        "session_binding_fingerprint": config.session_binding_fingerprint,
    }


def _restore(tmp_path: Path, config: Config, stamp: dict[str, object]):
    worktree = tmp_path / "repo-123"
    worktree.mkdir(exist_ok=True)
    run_assets = make_session_run_assets(worktree, session_name="issue-123")
    RUN_LEDGER.record(run_assets, SessionKind.CODE, "agent:web")
    (run_assets.run_dir / "session-identity.json").write_text(json.dumps(stamp), encoding="utf-8")
    repo_host = MockRepositoryHost()
    repo_host.issues[123] = Issue(number=123, title="Some task", labels=["agent:web"])
    working_copy = MockWorkingCopy()
    working_copy.branches[worktree] = "123-some-task"
    return restorer_for(config, repo_host, working_copy).restore_sessions(
        [DiscoveredSession(
            issue_number=123, tab_name="#123 Some task", is_review=False,
            session_name="issue-123", run_dir=str(run_assets.run_dir),
        )],
        already_tracked=[],
    )


@dataclass
class _NextVersionConfig(Config):
    """The next schema addition: a new defaulted field, and a changed default."""

    a_brand_new_setting: dict[str, int] = field(default_factory=lambda: {"knob": 1})
    max_concurrent_sessions: int = 99


# -- the fingerprints ------------------------------------------------------------


def test_a_new_defaulted_field_or_a_changed_default_leaves_an_unchanged_yaml_unchanged(
    tmp_path: Path,
) -> None:
    path = _config_path(tmp_path)
    today = Config.load(path)
    upgraded = _NextVersionConfig.load(path)

    assert _pre_upgrade_fingerprint(upgraded) != _pre_upgrade_fingerprint(today)  # the old hazard
    assert upgraded.config_fingerprint == today.config_fingerprint
    assert upgraded.session_binding_fingerprint == today.session_binding_fingerprint


@pytest.mark.parametrize(
    ("edit", "binding_moves"),
    [
        ("    model: opus\n", True),  # the session's own agent: binding
        ("  max_expedited: 5\n", False),  # applied live by a running engine
    ],
    ids=["agent-model", "live-setting"],
)
def test_an_operator_edit_moves_the_config_and_binds_only_restart_settings(
    tmp_path: Path, edit: str, binding_moves: bool
) -> None:
    before = Config.load(_config_path(tmp_path))
    edited_yaml = _YAML.replace("    model: sonnet\n", edit) if "model" in edit else (
        _YAML.replace("  max_expedited: 3\n", edit)
    )
    after = Config.load(_config_path(tmp_path, edited_yaml))

    assert after.config_fingerprint != before.config_fingerprint
    assert (after.session_binding_fingerprint != before.session_binding_fingerprint) is binding_moves


@pytest.mark.parametrize(
    ("attr", "binding_moves"),
    [("ui_mode", True), ("max_concurrent_sessions", False)],
)
def test_a_runtime_override_counts_as_operator_input(
    tmp_path: Path, attr: str, binding_moves: bool
) -> None:
    """CLI flags mutate the loaded config; they are configuration too."""
    config = Config.load(_config_path(tmp_path))
    before = (config.config_fingerprint, config.session_binding_fingerprint)
    setattr(config, attr, "subprocess" if attr == "ui_mode" else 7)

    config.refresh_config_fingerprint()

    assert config.config_fingerprint != before[0]
    assert (config.session_binding_fingerprint != before[1]) is binding_moves


# -- restore across an upgrade ---------------------------------------------------


def test_a_session_stamped_before_the_upgrade_restores_under_the_unchanged_yaml(
    tmp_path: Path,
) -> None:
    """The HIGH-risk breaker: io/porchpin restarting onto new code."""
    config = Config.load(_config_path(tmp_path))
    pre_upgrade_stamp = {
        "configuration_mode": "codex",
        "config_name": "main.yaml",
        "config_fingerprint": _pre_upgrade_fingerprint(config),
    }
    assert pre_upgrade_stamp["config_fingerprint"] != config.config_fingerprint

    restored = _restore(tmp_path, config, pre_upgrade_stamp)

    assert [session.terminal_id for session in restored] == ["issue-123"]


def test_a_session_restores_after_the_next_schema_addition(tmp_path: Path) -> None:
    path = _config_path(tmp_path)
    stamp = _launch_stamp(Config.load(path))

    restored = _restore(tmp_path, _NextVersionConfig.load(path), stamp)

    assert [session.terminal_id for session in restored] == ["issue-123"]


def test_a_session_restores_over_an_edit_to_a_live_applied_setting(tmp_path: Path) -> None:
    stamp = _launch_stamp(Config.load(_config_path(tmp_path)))
    edited = Config.load(_config_path(tmp_path, _YAML.replace("max_expedited: 3", "max_expedited: 5")))

    restored = _restore(tmp_path, edited, stamp)

    assert [session.terminal_id for session in restored] == ["issue-123"]


def test_editing_a_binding_setting_still_refuses_the_restore(tmp_path: Path) -> None:
    stamp = _launch_stamp(Config.load(_config_path(tmp_path)))
    edited = Config.load(_config_path(tmp_path, _YAML.replace("model: sonnet", "model: opus")))

    with pytest.raises(SessionConfigurationModeMismatchError, match="requires a restart"):
        _restore(tmp_path, edited, stamp)


def test_a_pre_upgrade_session_under_another_mode_is_still_refused(tmp_path: Path) -> None:
    config = Config.load(_config_path(tmp_path))
    stamp = {"configuration_mode": "claude", "config_name": "main.yaml", "config_fingerprint": "x" * 64}

    with pytest.raises(SessionConfigurationModeMismatchError):
        _restore(tmp_path, config, stamp)
