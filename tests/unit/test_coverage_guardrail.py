from issue_orchestrator.validation import GuardrailConfig, GuardrailDeps, run_guardrail


def test_run_guardrail_changed_filters_scope_and_exclude():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
        exclude=["src/issue_orchestrator/generated/**"],
    )
    deps = GuardrailDeps(
        get_changed_files=lambda: [
            "README.md",
            "src/issue_orchestrator/core.py",
            "src/issue_orchestrator/generated/schema.py",
        ],
        get_tracked_files=lambda: [],
        get_coverage_map=lambda: {"src/issue_orchestrator/core.py": 90.0},
    )

    result = run_guardrail(config, deps)

    assert result.status == "ok"
    assert result.candidates == ["src/issue_orchestrator/core.py"]


def test_run_guardrail_changed_skips_when_no_matches():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
    )
    deps = GuardrailDeps(
        get_changed_files=lambda: ["README.md"],
        get_tracked_files=lambda: [],
        get_coverage_map=lambda: {},
    )

    result = run_guardrail(config, deps)

    assert result.status == "skip"
    assert result.reason == "no changed files in scope"


def test_run_guardrail_all_errors_when_empty():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="all",
        scope=["src/issue_orchestrator/**"],
    )
    deps = GuardrailDeps(
        get_changed_files=lambda: [],
        get_tracked_files=lambda: ["README.md"],
        get_coverage_map=lambda: {},
    )

    result = run_guardrail(config, deps)

    assert result.status == "error"
    assert result.reason == "no tracked files matched scope"


def test_run_guardrail_reports_failures():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85.0,
        apply_to="changed",
        scope=["*.py"],
    )
    deps = GuardrailDeps(
        get_changed_files=lambda: ["a.py", "b.py", "c.py"],
        get_tracked_files=lambda: [],
        get_coverage_map=lambda: {"a.py": 90.0, "b.py": 80.0},
    )

    result = run_guardrail(config, deps)

    assert result.status == "fail"
    assert [f.path for f in result.failures] == ["b.py", "c.py"]
    assert result.failures[0].percent == 80.0
    assert result.failures[1].percent is None
