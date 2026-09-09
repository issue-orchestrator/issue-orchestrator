from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "stack_ci_validation.py"
SPEC = importlib.util.spec_from_file_location("stack_ci_validation", SCRIPT_PATH)
assert SPEC is not None
stack_ci_validation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = stack_ci_validation
SPEC.loader.exec_module(stack_ci_validation)


def _stacked_pull_request(
    *, position: object = 2, size: object = 3, direct_base: object = "layer-1"
) -> dict[str, object]:
    return {
        "pull_request": {
            "base": {"ref": direct_base},
            "stack": {
                "position": position,
                "size": size,
                "base": {"ref": "main"},
            },
        }
    }


@pytest.mark.parametrize("event_name", ["merge_group", "push", "workflow_dispatch"])
def test_non_pull_request_events_always_run_full_validation(event_name: str) -> None:
    decision = stack_ci_validation.decide_stack_ci_validation(event_name, {})

    assert decision.run_full is True


def test_standalone_pull_request_runs_full_validation() -> None:
    decision = stack_ci_validation.decide_stack_ci_validation(
        "pull_request", {"pull_request": {"base": {"ref": "main"}}}
    )

    assert decision.run_full is True


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        (_stacked_pull_request(direct_base="main"), "lowest"),
        (_stacked_pull_request(position=3), "top"),
    ],
)
def test_stack_landing_boundaries_run_full_validation(
    payload: dict[str, object], expected_reason: str
) -> None:
    decision = stack_ci_validation.decide_stack_ci_validation("pull_request", payload)

    assert decision.run_full is True
    assert expected_reason in decision.reason


def test_intermediate_stack_layer_defers_aggregate_validation() -> None:
    decision = stack_ci_validation.decide_stack_ci_validation(
        "pull_request", _stacked_pull_request()
    )

    assert decision.run_full is False
    assert "intermediate" in decision.reason


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"pull_request": []},
        {"pull_request": {"base": {"ref": "layer-1"}, "stack": []}},
        _stacked_pull_request(position=0),
        _stacked_pull_request(position=True),
        _stacked_pull_request(position=1),
        _stacked_pull_request(position=4),
        _stacked_pull_request(size="3"),
        _stacked_pull_request(direct_base=""),
    ],
)
def test_missing_or_invalid_stack_metadata_runs_full_validation(
    payload: dict[str, object],
) -> None:
    decision = stack_ci_validation.decide_stack_ci_validation("pull_request", payload)

    assert decision.run_full is True


def test_cli_emits_github_action_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "output"
    event_path.write_text(json.dumps(_stacked_pull_request()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    assert stack_ci_validation.main() == 0
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "run_full=false",
        "reason=aggregate validation is deferred until this intermediate layer "
        "becomes the lowest unmerged layer; the cumulative top layer is validated now",
    ]


def test_cli_fails_closed_without_the_github_output_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_stacked_pull_request()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    with pytest.raises(RuntimeError, match="refusing to defer"):
        stack_ci_validation.main()
