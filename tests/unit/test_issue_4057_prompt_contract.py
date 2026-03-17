from tests.e2e.test_issue_4057_production_flow import (
    CODING_AGENT_TIMEOUT_MINUTES,
    E2E_TIMEOUT_MINUTES,
    FOLLOW_UP_FILE_PATH,
    ISSUE_4057_PROMPT,
    REVIEW_AGENT_TIMEOUT_MINUTES,
    build_issue_4057_body,
)


def test_4057_prompt_enforces_time_bounded_scope_and_follow_up_file():
    assert f"time-bounded to {CODING_AGENT_TIMEOUT_MINUTES} minutes" in ISSUE_4057_PROMPT
    assert "src/issue_orchestrator/view_models/dashboard.py" in ISSUE_4057_PROMPT
    assert "tests/unit/test_dashboard_view_model.py" in ISSUE_4057_PROMPT
    assert "pytest tests/unit/test_dashboard_view_model.py -q" in ISSUE_4057_PROMPT
    assert f"--follow-up-file {FOLLOW_UP_FILE_PATH}" in ISSUE_4057_PROMPT
    assert "Do not look up or reference other issue numbers." in ISSUE_4057_PROMPT


def test_4057_issue_body_matches_focus_contract():
    body = build_issue_4057_body()
    assert "First inspect src/issue_orchestrator/view_models/dashboard.py and tests/unit/test_dashboard_view_model.py only" in body
    assert "Validation must run through make validate-quick" in body
    assert f"Record unrelated ancillary work in {FOLLOW_UP_FILE_PATH}" in body
    assert "Complete via coding-done and exit" in body


def test_4057_e2e_timeout_exceeds_agent_budgets():
    assert E2E_TIMEOUT_MINUTES > CODING_AGENT_TIMEOUT_MINUTES
    assert E2E_TIMEOUT_MINUTES > CODING_AGENT_TIMEOUT_MINUTES + REVIEW_AGENT_TIMEOUT_MINUTES
