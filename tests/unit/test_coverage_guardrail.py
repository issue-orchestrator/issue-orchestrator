from issue_orchestrator.validation import GuardrailConfig, GuardrailDeps, run_guardrail


def _deps(changed, tracked, coverage_map):
    return GuardrailDeps(
        get_changed_files=lambda: changed,
        get_tracked_files=lambda: tracked,
        get_coverage_map=lambda: coverage_map,
    )


def test_run_guardrail_filters_scope_and_exclude():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
        exclude=["src/issue_orchestrator/generated/**"],
    )
    deps = _deps(
        changed=[
            "README.md",
            "src/issue_orchestrator/core.py",
            "src/issue_orchestrator/generated/schema.py",
        ],
        tracked=[],
        coverage_map={"src/issue_orchestrator/core.py": 90.0},
    )

    result = run_guardrail(config, deps)

    assert result.status == "ok"
    assert result.reason is None
    assert result.candidates == ["src/issue_orchestrator/core.py"]
    assert result.failures == []


def test_run_guardrail_skips_when_no_matches():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/issue_orchestrator/**"],
    )
    deps = _deps(
        changed=["README.md"],
        tracked=[],
        coverage_map={},
    )

    result = run_guardrail(config, deps)

    assert result.status == "skip"
    assert result.reason == "no changed files in scope"
    assert result.candidates == []
    assert result.failures == []


def test_run_guardrail_all_errors_when_empty():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="all",
        scope=["src/issue_orchestrator/**"],
    )
    deps = _deps(
        changed=[],
        tracked=["README.md"],
        coverage_map={},
    )

    result = run_guardrail(config, deps)

    assert result.status == "error"
    assert result.reason == "no tracked files matched scope"
    assert result.candidates == []
    assert result.failures == []


def test_run_guardrail_reports_failures():
    config = GuardrailConfig(
        enabled=True,
        min_percent=85,
        apply_to="changed",
        scope=["src/**"],
    )
    deps = _deps(
        changed=["src/a.py", "src/b.py", "src/c.py"],
        tracked=[],
        coverage_map={"src/a.py": 90.0, "src/b.py": 80.0},
    )

    result = run_guardrail(config, deps)

    assert result.status == "fail"
    assert [failure.path for failure in result.failures] == ["src/b.py", "src/c.py"]
    assert result.failures[0].percent == 80.0
    assert result.failures[1].percent is None
