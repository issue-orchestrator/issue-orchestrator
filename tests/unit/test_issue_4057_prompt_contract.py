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
    assert "production-flow E2E control-path check" in ISSUE_4057_PROMPT
    assert "Open only tests/unit/test_dashboard_view_model.py." in ISSUE_4057_PROMPT
    assert "test_normalize_status_reason_drops_none_and_blank_values" in ISSUE_4057_PROMPT
    assert "Do NOT edit production files, generated contracts, schemas, `.gitignore`, or any other tests." in ISSUE_4057_PROMPT
    assert "tests/unit/test_dashboard_view_model.py" in ISSUE_4057_PROMPT
    assert "pytest tests/unit/test_dashboard_view_model.py -q" in ISSUE_4057_PROMPT
    assert f"--follow-up-file {FOLLOW_UP_FILE_PATH}" in ISSUE_4057_PROMPT
    assert "Do not look up or reference other issue numbers." in ISSUE_4057_PROMPT


def test_4057_issue_body_matches_focus_contract():
    body = build_issue_4057_body()
    assert "Treat this as a control-flow verification, not a feature implementation task" in body
    assert "Open only tests/unit/test_dashboard_view_model.py" in body
    assert "Add exactly one regression test named test_normalize_status_reason_drops_none_and_blank_values" in body
    assert "Final validation must run through pytest tests/unit/test_dashboard_view_model.py -q" in body
    assert f"Record unrelated ancillary work in {FOLLOW_UP_FILE_PATH}" in body
    assert "Complete via coding-done and exit" in body


def test_4057_e2e_timeout_exceeds_agent_budgets():
    assert E2E_TIMEOUT_MINUTES > CODING_AGENT_TIMEOUT_MINUTES
    assert E2E_TIMEOUT_MINUTES > CODING_AGENT_TIMEOUT_MINUTES + REVIEW_AGENT_TIMEOUT_MINUTES
