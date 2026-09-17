"""The mode->guard-label choice has one owner that refuses the unknown (#7263).

Two copies of this map existed -- the launcher's and completion planning's --
and both treated every non-"coding" value as review. A typo or a third mode
would have silently cleared the wrong guard, and a guard cleared on the wrong
issue is a relaunch loop nothing stops.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.infra.config_models import InterruptedSessionRetryConfig


def _config() -> InterruptedSessionRetryConfig:
    return InterruptedSessionRetryConfig(
        coding_guard_label="io:coding-guard",
        review_guard_label="io:review-guard",
    )


def test_each_supported_mode_maps_to_its_own_label() -> None:
    config = _config()

    assert config.guard_label("coding") == "io:coding-guard"
    assert config.guard_label("review") == "io:review-guard"


def test_an_unsupported_mode_is_refused_rather_than_treated_as_review() -> None:
    with pytest.raises(ValueError, match="no interrupted-retry guard label"):
        _config().guard_label("rework")
