"""The mode->guard-label choice has one owner that refuses the unknown (#7263).

Two copies of this map existed -- the launcher's and completion planning's --
and both treated every non-"coding" value as review. A typo or a third mode
would have silently cleared the wrong guard, and a guard cleared on the wrong
issue is a relaunch loop nothing stops.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from issue_orchestrator.infra.config_models import InterruptedSessionRetryConfig

SRC = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"

#: The modes the owner answers for. Anything else raises.
SUPPORTED_MODES = {"coding", "review"}


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


class TestTheRefusalIsUnreachableFromProduction:
    """The narrowing is not a behaviour change: nothing reaches the raise.

    Refusing an unknown mode is observably different from the old silent
    fall-through, so the claim that this PR changes no behaviour rests on
    every production caller passing a mode this owner supports. That is a
    static property of the call sites, so it is checked statically -- a new
    launch path spelling its mode differently fails here rather than at
    3 a.m. on a relaunch.
    """

    def _source(self, relative: str) -> ast.Module:
        path = SRC / relative
        return ast.parse(path.read_text(), filename=str(path))

    def test_every_launch_path_passes_a_supported_mode(self) -> None:
        passed: list[str] = []
        for relative in (
            "control/session_launcher.py",
            "control/session_rework_launcher.py",
        ):
            for node in ast.walk(self._source(relative)):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name not in ("_clear_launch_retry_guards", "clear_launch_retry_guards"):
                    continue
                mode = next(
                    (kw.value for kw in node.keywords if kw.arg == "mode"), None
                )
                assert isinstance(mode, ast.Constant), (
                    f"{relative}:{node.lineno} passes a non-literal mode; the "
                    "guard-label owner refuses anything it does not know"
                )
                passed.append(mode.value)

        assert passed, "no launch path was found; this guard stopped guarding"
        assert set(passed) <= SUPPORTED_MODES

    def test_completion_planning_derives_only_supported_modes(self) -> None:
        """The other former copy of the map, pinned at its source.

        Every return is read, not just the literal ones: a derivation that grew
        ``return session.mode`` beside its existing literals would keep this
        green while an unsupported value reached the owner and raised during
        completion processing.
        """
        module = self._source("control/completion_action_planner.py")
        derivation = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_interrupted_retry_mode"
        )

        returns = [
            node for node in ast.walk(derivation) if isinstance(node, ast.Return)
        ]

        assert returns, "the derivation returns nothing; re-read it"
        for node in returns:
            assert node.value is None or isinstance(node.value, ast.Constant), (
                f"completion_action_planner.py:{node.lineno} returns a computed "
                "mode; the guard-label owner refuses anything it does not know"
            )
            value = None if node.value is None else node.value.value
            assert value in SUPPORTED_MODES | {None}
