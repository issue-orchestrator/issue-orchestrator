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
        staged.CASE_FILES_FILE, staged.INTERVENTIONS_FILE, staged.OPEN_ISSUES_FILE, staged.BLOCKED_ITEMS_FILE,
        staged.INPUTS_FILE, f"{staged.ENGINE_SOURCE_DIRNAME}/", f"{staged.EXAM_DIRNAME}/<case id>.json",
        f"{staged.EXAM_DIRNAME}/<case id>{staged.PREVIOUS_SCORECARD_SUFFIX}",
    ):
        assert f"`{name}`" in table, name


def test_the_prompt_points_at_the_examples_the_validator_accepts() -> None:
    assert "engine-source/examples/improver/findings/" in PROMPT.read_text(encoding="utf-8")
    assert (PROMPT.parents[1] / "improver" / "findings").is_dir()


def test_the_prompt_names_the_schema_version_the_validator_reads() -> None:
    from issue_orchestrator.contracts.improver_findings import IMPROVER_FINDINGS_SCHEMA_VERSION

    assert f'"schema_version": {IMPROVER_FINDINGS_SCHEMA_VERSION},' in PROMPT.read_text(encoding="utf-8")


def test_the_prompt_makes_every_blocked_item_accounted_for_and_a_diagnosis_a_finding() -> None:
    """Handover grade #1: the blind run dropped live blocked items as history
    and as already diagnosed. The prompt forbids both."""
    text = " ".join(PROMPT.read_text(encoding="utf-8").split())
    assert "**Account for every blocked item (the objective).**" in text
    assert "**regardless of origin**" in text
    assert "**A diagnosis is not a fix**" in text
    assert '"blocked_items": [' in text
