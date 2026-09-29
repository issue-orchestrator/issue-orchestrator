"""The improver prompt and the orchestrator agree on what is staged (#7490)."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.contracts import improver_inputs as staged

PROMPT = Path(__file__).resolve().parents[2] / "examples" / "prompts" / "tech-lead-improver.md"


def test_the_inputs_table_names_every_file_the_staging_writes() -> None:
    table = PROMPT.read_text(encoding="utf-8").split("## Method", 1)[0]
    for name in (
        staged.AUDIT_FILE, staged.AUDIT_PREVIOUS_FILE, staged.AUDIT_DIFF_FILE,
        staged.ENGINE_START_FILE, staged.CHARTER_FILE, staged.CHARTER_DECISIONS_FILE,
        staged.CASE_FILES_FILE, staged.INTERVENTIONS_FILE, staged.OPEN_ISSUES_FILE,
        staged.INPUTS_FILE, f"{staged.ENGINE_SOURCE_DIRNAME}/", f"{staged.EXAM_DIRNAME}/<case id>.json",
        f"{staged.EXAM_DIRNAME}/<case id>{staged.PREVIOUS_SCORECARD_SUFFIX}",
    ):
        assert f"`{name}`" in table, name


def test_the_prompt_points_at_the_examples_the_validator_accepts() -> None:
    assert "engine-source/examples/improver/findings/" in PROMPT.read_text(encoding="utf-8")
    assert (PROMPT.parents[1] / "improver" / "findings").is_dir()
