"""``improver validate``: the budgeted-validation exit codes (#7490)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import improver
from tests.unit.improver_support import build_improver_data, example


def test_a_valid_findings_file_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_improver_data(tmp_path)
    (tmp_path / improver.FINDINGS_FILE).write_text(json.dumps(example("exam_case")))

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_OK
    assert "valid: 1 finding(s), 0 design finding(s)" in capsys.readouterr().out


def test_a_rejected_findings_file_exits_one_naming_each_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_improver_data(tmp_path)
    doc = example("exam_case")
    doc["findings"][0]["reproduction"]["fails_on"] = "HEAD"
    (tmp_path / improver.FINDINGS_FILE).write_text(json.dumps(doc))

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_REJECTED
    assert "[reproduction_fails_on_engine_commit]" in capsys.readouterr().out


def test_no_findings_file_is_a_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_improver_data(tmp_path)

    assert improver.main(["validate", "--run-dir", str(tmp_path)]) == improver.EXIT_REJECTED
    assert "wrote no improver-findings.json" in capsys.readouterr().out


def test_staging_an_engine_without_a_start_record_exits_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Host:
        def list_open_issue_labels_complete(self):  # pragma: no cover - never reached
            raise AssertionError

    monkeypatch.setattr(improver, "create_repository_host", lambda repo: Host())
    state = tmp_path / "repo" / ".issue-orchestrator" / "state"
    state.mkdir(parents=True)

    code = improver.main([
        "stage", "--state-dir", str(state), "--audited-repo", "o/r", "--outputs-repo", "o/r",
        "--run-dir", str(tmp_path / "run"), "--no-github",
    ])

    assert code == improver.EXIT_UNAVAILABLE


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Unset: the latest improver tournament's winner (#8001).
        ([], "claude:opus"),
        (["--provider", "codex"], "codex:gpt-5.6-sol"),
        (["--provider", "claude", "--model", "sonnet"], "claude:sonnet"),
        (["--model", "fable"], "claude:fable"),
    ],
)
def test_run_launches_the_chosen_provider_and_model(argv: list[str], expected: str) -> None:
    args = improver.settle(improver.build_parser().parse_args(["run", "--outputs-repo", "o/r", *argv]), None)

    assert improver.agent_choice(args).describe() == expected


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(SystemExit):
        improver.build_parser().parse_args(["run", "--outputs-repo", "o/r", "--provider", "gemini"])


def test_a_run_is_empowered_and_dry_by_default() -> None:
    args = improver.settle(improver.build_parser().parse_args(["run", "--outputs-repo", "o/r"]), None)

    assert args.mode is improver.ImproverMode.EMPOWERED
    assert args.apply is False
    assert isinstance(improver._investigation(args), improver.EmpoweredInvestigation)


def test_a_scripted_run_has_no_toolbox() -> None:
    args = improver.build_parser().parse_args(["run", "--outputs-repo", "o/r", "--mode", "scripted"])

    assert isinstance(improver._investigation(args), improver.ScriptedInvestigation)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--budget-minutes", "90", "--agent-timeout-minutes", "90"], "below --agent-timeout-minutes"),
        (["--exclude-open-issue", "7", "--apply"], "cannot --apply"),
    ],
)
def test_contradictory_run_options_are_refused(argv: list[str], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        improver.main(["run", "--outputs-repo", "o/r", "--state-dir", "/x/.issue-orchestrator/state",
                       "--audited-repo", "o/r", *argv])


def test_a_run_sends_two_heats_at_once_by_default_and_refuses_none() -> None:
    """#8001: modest by default; each heat costs a whole agent run."""
    args = improver.settle(improver.build_parser().parse_args(["run", "--outputs-repo", "o/r"]), None)

    assert (args.heats, args.parallel_heats, args.run_budget_minutes) == (2, 2, 105)
    for argv in (["--heats", "0"], ["--parallel-heats", "0"], ["--heats", "6"], ["--heats", "2", "--parallel-heats", "3"]):
        with pytest.raises(SystemExit, match="--heats/--parallel-heats"):
            improver.main(["run", "--outputs-repo", "o/r", "--state-dir", "/x/.issue-orchestrator/state",
                           "--audited-repo", "o/r", *argv])



def test_queued_heats_must_fit_the_run_budget() -> None:
    """r1 F4: two waves of 90-minute heats would outlive the suite's timeout."""
    with pytest.raises(SystemExit, match="exceeds the 105-minute run budget"):
        improver.main(["run", "--outputs-repo", "o/r", "--state-dir", "/x/.issue-orchestrator/state",
                       "--audited-repo", "o/r", "--heats", "4", "--parallel-heats", "2"])



def _champion(tmp_path: Path) -> improver.Champion:
    from issue_orchestrator.contracts.improver_variant import ImproverVariant
    from issue_orchestrator.domain.improver_champion import prompt_digest

    prompt = "THE CHAMPION'S PROMPT"
    return improver.Champion(ImproverVariant(
        agent=improver.ImproverAgentChoice(provider=improver.ImproverProvider.CODEX, model="gpt-5.6-sol"),
        mode=improver.ImproverMode.SCRIPTED, heats=1, budget_minutes=45, prompt_sha256=prompt_digest(prompt),
    ), prompt)


def test_an_unset_run_runs_the_champion(tmp_path: Path) -> None:
    champion = _champion(tmp_path)

    args = improver.settle(improver.build_parser().parse_args(["run", "--outputs-repo", "o/r"]), champion)

    assert improver.agent_choice(args).describe() == "codex:gpt-5.6-sol"
    assert (args.mode, args.heats, args.parallel_heats, args.budget_minutes) == (
        improver.ImproverMode.SCRIPTED, 1, 1, 45,
    )
    assert args.prompt_text == "THE CHAMPION'S PROMPT" and args.runs_champion


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--provider", "claude"], "claude:opus"),  # another provider: its own default model
        (["--model", "gpt-5.5"], "codex:gpt-5.5"),
        (["--heats", "2"], "codex:gpt-5.6-sol"),
    ],
)
def test_a_run_that_sets_any_improver_setting_is_not_the_champion(argv: list[str], expected: str, tmp_path: Path) -> None:
    args = improver.settle(improver.build_parser().parse_args(["run", "--outputs-repo", "o/r", *argv]), _champion(tmp_path))

    assert improver.agent_choice(args).describe() == expected
    assert not args.runs_champion
