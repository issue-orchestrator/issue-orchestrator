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

    assert [(r.arm.name, r.arm.provider, r.arm.model, r.arm.mode) for r in args.arm] == [
        ("C", "claude", "opus", "empowered"), ("A", "codex", "gpt-5.6-sol", "scripted"),
    ]
    assert args.grader[0].name == "g2" and args.grader[0].choice.describe() == "codex:gpt-5.6-sol"
    assert (args.heats, args.parallel_heats) == (3, 3)


@pytest.mark.parametrize("arm", [
    "C=claude:opus", "C=gemini:x:scripted", "=claude:opus:scripted", "C=claude:opus:wild",
    "C=claude:opus:empowered,colour=red", "C=claude:opus:empowered,heats=two", "C=claude:opus:empowered,heats=2,heats=3",
    "C=claude:opus:empowered,budget=",
])
def test_a_malformed_arm_is_refused(arm: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_arm(arm)


def test_recorded_answers_are_read_as_arm_and_heat(tmp_path: Path) -> None:
    for name, text in (("A1.txt", "{}"), ("C3.txt", '{"findings": []}'), ("D2.txt", "  ")):
        (tmp_path / name).write_text(text)

    outputs = cli.read_recorded(tmp_path)

    assert [(o.arm, o.heat, o.text) for o in outputs] == [("A", 1, "{}"), ("C", 3, '{"findings": []}'), ("D", 2, None)]


def test_a_recorded_file_that_names_no_arm_and_heat_is_refused(tmp_path: Path) -> None:
    (tmp_path / "MAPPING.txt").write_text("x")
    with pytest.raises(SystemExit, match="not <arm><heat>.txt"):
        cli.read_recorded(tmp_path)


def test_a_challenger_arm_sets_its_own_prompt_heats_and_minutes(tmp_path: Path) -> None:
    challenger = tmp_path / "challenger.md"
    challenger.write_text("CHALLENGER")
    champion = tmp_path / "champion.md"
    champion.write_text("CHAMPION")
    args = cli.build_parser().parse_args([
        "run", "--snapshot", "s", "--arm", "A=claude:opus:empowered",
        "--arm", f"B=claude:opus:empowered,prompt={challenger},heats=2,parallel=1,budget=45,timeout=55",
    ])

    a, b = (request.spec(args, prompt=champion, addendum="ADD") for request in args.arm)

    assert (a.prompt, a.heats.count, a.heats.parallel, a.budget_minutes, a.agent_timeout_minutes) == ("CHAMPION", 3, 3, 60, 80)
    assert (b.prompt, b.heats.count, b.heats.parallel, b.budget_minutes, b.agent_timeout_minutes) == ("CHALLENGER", 2, 1, 45, 55)
    over = cli.build_parser().parse_args(["run", "--snapshot", "s", "--arm", "C=claude:opus:empowered,budget=90"])
    with pytest.raises(ValueError, match="budget must be below"):
        over.arm[0].spec(over, prompt=champion, addendum="ADD")


@pytest.mark.parametrize("grader", ["../../snapshots/x=claude:opus", "a/b=codex:m"])
def test_a_grader_name_is_one_path_component(grader: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="one path component"):
        cli.parse_grader(grader)
