from issue_orchestrator.validation.coverage_guardrail import (
    GuardrailConfig,
    evaluate_coverage,
    select_candidates,
)


def test_select_candidates_changed_filters_scope_and_exclude():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
        exclude=["src/issue_orchestrator/generated/**"],
    )
    changed = [
        "README.md",
        "src/issue_orchestrator/core.py",
        "src/issue_orchestrator/generated/schema.py",
    ]

    selection = select_candidates(config, changed_files=changed, tracked_files=[])

    assert selection.error is None
    assert selection.skip_reason is None
    assert selection.candidates == ["src/issue_orchestrator/core.py"]


def test_select_candidates_changed_skips_when_no_matches():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
    )

    selection = select_candidates(config, changed_files=["README.md"], tracked_files=[])

    assert selection.error is None
    assert selection.skip_reason == "no changed files in scope"
    assert selection.candidates == []


def test_select_candidates_all_errors_when_empty():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="all",
        scope=["src/issue_orchestrator/**"],
    )

    selection = select_candidates(config, changed_files=[], tracked_files=["README.md"])

    assert selection.error == "no tracked files matched scope"
    assert selection.candidates == []


def test_evaluate_coverage_reports_failures():
    failures = evaluate_coverage(
        candidates=["a.py", "b.py", "c.py"],
        coverage_map={"a.py": 90.0, "b.py": 80.0},
        min_percent=85.0,
    )

    assert [f.path for f in failures] == ["b.py", "c.py"]
    assert failures[0].percent == 80.0
    assert failures[1].percent is None
