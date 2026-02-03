from issue_orchestrator.validation import GuardrailConfig, evaluate_guardrail


def test_evaluate_guardrail_filters_scope_and_exclude():
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

    result = evaluate_guardrail(
        config=config,
        changed_files=changed,
        tracked_files=[],
        coverage_map={"src/issue_orchestrator/core.py": 90.0},
    )

    assert result.selection.error is None
    assert result.selection.skip_reason is None
    assert result.selection.candidates == ["src/issue_orchestrator/core.py"]
    assert result.failures == []


def test_evaluate_guardrail_skips_when_no_matches():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
    )

    result = evaluate_guardrail(
        config=config,
        changed_files=["README.md"],
        tracked_files=[],
        coverage_map={},
    )

    assert result.selection.error is None
    assert result.selection.skip_reason == "no changed files in scope"
    assert result.selection.candidates == []
    assert result.failures == []


def test_evaluate_guardrail_all_errors_when_empty():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="all",
        scope=["src/issue_orchestrator/**"],
    )

    result = evaluate_guardrail(
        config=config,
        changed_files=[],
        tracked_files=["README.md"],
        coverage_map={},
    )

    assert result.selection.error == "no tracked files matched scope"
    assert result.selection.candidates == []
    assert result.failures == []


def test_evaluate_guardrail_reports_failures():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/**"],
    )

    result = evaluate_guardrail(
        config=config,
        changed_files=["src/a.py", "src/b.py", "src/c.py"],
        tracked_files=[],
        coverage_map={"src/a.py": 90.0, "src/b.py": 80.0},
    )

    assert [failure.path for failure in result.failures] == ["src/b.py", "src/c.py"]
    assert result.failures[0].percent == 80.0
    assert result.failures[1].percent is None
