"""``tech_lead.custody.stale_after_minutes`` (#7331): shape, bounds, settings form."""

from __future__ import annotations

from datetime import timedelta

import pytest

from issue_orchestrator.domain.blocked_item_custody import OWNED_CUSTODY_STATES, CustodyState
from issue_orchestrator.infra.config_models_tech_lead_custody import (
    DEFAULT_CUSTODY_STALE_MINUTES,
    MAX_CUSTODY_STALE_MINUTES,
    CustodyStaleAfterMinutes,
    TechLeadCustodyConfig,
)
from issue_orchestrator.infra.config_sections_tech_lead import parse_tech_lead_config
from issue_orchestrator.infra.settings_schema import ReviewSettings


def _parse(custody: object) -> TechLeadCustodyConfig:
    return parse_tech_lead_config({"findings": {}, "custody": custody}).custody


def test_every_owned_state_has_a_default_threshold() -> None:
    thresholds = _parse(None).stale_after_minutes.to_thresholds()

    for state in OWNED_CUSTODY_STATES:
        assert thresholds.for_state(state) == timedelta(
            minutes=DEFAULT_CUSTODY_STALE_MINUTES[state]
        )


def test_a_state_left_out_keeps_its_default() -> None:
    minutes = _parse({"stale_after_minutes": {"held": 60}}).stale_after_minutes

    assert minutes.held == 60
    assert minutes.verify == DEFAULT_CUSTODY_STALE_MINUTES[CustodyState.VERIFY]


@pytest.mark.parametrize(
    ("custody", "message"),
    [
        ("often", "tech_lead.custody must be a mapping"),
        ({"stale_minutes": {}}, "unknown key"),
        ({"stale_after_minutes": {"unowned": 5}}, "unknown state"),
        ({"stale_after_minutes": {"held": "1h"}}, "whole number of minutes"),
        ({"stale_after_minutes": {"held": True}}, "whole number of minutes"),
        ({"stale_after_minutes": [1]}, "must be a mapping"),
    ],
)
def test_a_malformed_block_fails_loudly(custody: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse(custody)


@pytest.mark.parametrize("minutes", [0, -5, MAX_CUSTODY_STALE_MINUTES + 1])
def test_an_out_of_range_threshold_is_a_startup_error(minutes: int) -> None:
    custody = _parse({"stale_after_minutes": {"investigating": minutes}})

    assert custody.startup_errors() == [
        "tech_lead.custody.stale_after_minutes.investigating must be between 1 and"
        f" {MAX_CUSTODY_STALE_MINUTES} minutes, got {minutes}"
    ]
    with pytest.raises(ValueError):
        custody.stale_after_minutes.to_thresholds()


def test_the_tech_lead_block_reports_custody_errors() -> None:
    config = parse_tech_lead_config(
        {"findings": {}, "custody": {"stale_after_minutes": {"verify": 0}}}
    )

    assert any("stale_after_minutes.verify" in error for error in config.startup_errors())
    assert config.to_event_dict()["custody"] == {
        "stale_after_minutes": {
            **{state.value: DEFAULT_CUSTODY_STALE_MINUTES[state] for state in OWNED_CUSTODY_STATES},
            "verify": 0,
        }
    }


def test_the_settings_form_has_one_bounded_field_per_owned_state() -> None:
    fields = ReviewSettings.model_fields
    for state in OWNED_CUSTODY_STATES:
        field = fields[f"tech_lead_custody_stale_{state.value}_minutes"]
        extra = field.json_schema_extra
        assert isinstance(extra, dict)
        assert extra["config_attr"] == f"tech_lead.custody.stale_after_minutes.{state.value}"
        assert extra["yaml_path"] == extra["config_attr"]
        assert field.default == DEFAULT_CUSTODY_STALE_MINUTES[state]
    assert "tech_lead_custody_stale_unowned_minutes" not in fields


def test_the_dataclass_mirrors_the_owned_states_exactly() -> None:
    names = {name for name in CustodyStaleAfterMinutes.__dataclass_fields__}
    assert names == {state.value for state in OWNED_CUSTODY_STATES}
