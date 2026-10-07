"""The tournament's command line: arms, graders and recorded answers (#8001)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import improver_tournament as cli


def test_an_arm_and_a_grader_are_named_by_provider_model_and_mode() -> None:
    args = cli.build_parser().parse_args([
        "run", "--snapshot", "20261004", "--arm", "C=claude:opus:empowered", "--arm", "A=codex:gpt-5.6-sol:scripted",
        "--grader", "g2=codex:gpt-5.6-sol",
    ])

    assert [(a.name, a.provider, a.model, a.mode) for a in args.arm] == [
        ("C", "claude", "opus", "empowered"), ("A", "codex", "gpt-5.6-sol", "scripted"),
    ]
    assert args.grader[0].name == "g2" and args.grader[0].choice.describe() == "codex:gpt-5.6-sol"
    assert (args.heats, args.parallel_heats) == (3, 3)


@pytest.mark.parametrize("arm", ["C=claude:opus", "C=gemini:x:scripted", "=claude:opus:scripted", "C=claude:opus:wild"])
def test_a_malformed_arm_is_refused(arm: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._arm(arm)


def test_recorded_answers_are_read_as_arm_and_heat(tmp_path: Path) -> None:
    for name, text in (("A1.txt", "{}"), ("C3.txt", '{"findings": []}'), ("D2.txt", "  ")):
        (tmp_path / name).write_text(text)

    outputs = cli._recorded(tmp_path)

    assert [(o.arm, o.heat, o.text) for o in outputs] == [("A", 1, "{}"), ("C", 3, '{"findings": []}'), ("D", 2, None)]


def test_a_recorded_file_that_names_no_arm_and_heat_is_refused(tmp_path: Path) -> None:
    (tmp_path / "MAPPING.txt").write_text("x")
    with pytest.raises(SystemExit, match="not <arm><heat>.txt"):
        cli._recorded(tmp_path)
