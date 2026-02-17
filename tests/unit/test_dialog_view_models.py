from issue_orchestrator.view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
)


def _rows_to_map(rows):
    return {row["label"]: row["value"] for row in rows}


def test_build_info_dialog_defaults():
    dialog = build_info_dialog({})

    assert dialog["title"] == "About Issue Orchestrator"
    rows = _rows_to_map(dialog["rows"])
    assert rows["Version"] == "dev"
    assert rows["Commit"] == "unknown"
    assert rows["Max Sessions"] == "-"
    assert rows["Active Sessions"] == "0"


def test_build_info_dialog_values():
    dialog = build_info_dialog(
        {
            "version": "1.2.3",
            "repo": "repo",
            "ui_mode": "web",
            "terminal_backend": "tmux",
            "commit_short": "abcd123",
            "max_sessions": 3,
            "active_sessions": 2,
            "completed_today": 5,
        }
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Version"] == "1.2.3"
    assert rows["Repository"] == "repo"
    assert rows["UI Mode"] == "web"
    assert rows["Terminal"] == "tmux"
    assert rows["Commit"] == "abcd123"
    assert rows["Max Sessions"] == "3"
    assert rows["Active Sessions"] == "2"
    assert rows["Completed Today"] == "5"


def test_build_config_dialog():
    dialog = build_config_dialog("config text")

    assert dialog == {
        "title": "Configuration",
        "config_text": "config text",
    }


def test_build_debug_dialog_sections():
    dialog = build_debug_dialog(
        {
            "startup_options": {
                "ui_mode": "web",
                "web_port": 8080,
                "test_mode": True,
                "filtering": {
                    "label": "bug",
                    "milestone": "v1",
                },
                "max_sessions": 4,
            },
            "paused": False,
            "priority_queue": ["M1-1", "M1-2"],
            "config_path": "/config/path",
            "repo_root": "/repo/root",
            "agents": {"default": {"timeout": 15}},
        }
    )

    assert dialog["title"] == "Debug Info"
    sections = {section["title"]: section for section in dialog["sections"]}

    startup_rows = _rows_to_map(sections["Startup Options"]["rows"])
    assert startup_rows["UI Mode"] == "web"
    assert startup_rows["Web Port"] == "8080"
    assert startup_rows["Test Mode"] == "yes"
    assert startup_rows["Filter Label"] == "bug"
    assert startup_rows["Filter Milestone"] == "v1"
    assert startup_rows["Max Sessions"] == "4"

    state_rows = _rows_to_map(sections["State"]["rows"])
    assert state_rows["Paused"] == "False"
    assert state_rows["Priority Queue"] == "M1-1, M1-2"

    path_rows = _rows_to_map(sections["Paths"]["rows"])
    assert path_rows["Config Path"] == "/config/path"
    assert path_rows["Repo Root"] == "/repo/root"

    agent_rows = _rows_to_map(sections["Agent Types"]["rows"])
    assert agent_rows["default"] == "timeout: 15m"


def test_build_debug_dialog_defaults():
    dialog = build_debug_dialog({})

    sections = {section["title"]: section for section in dialog["sections"]}
    startup_rows = _rows_to_map(sections["Startup Options"]["rows"])
    assert startup_rows["Filter Label"] == "none"
    assert startup_rows["Filter Milestone"] == "none"

    state_rows = _rows_to_map(sections["State"]["rows"])
    assert state_rows["Priority Queue"] == "empty"


def test_build_doctor_dialog():
    dialog = build_doctor_dialog(
        {
            "overall": "warning",
            "checks": [
                {"name": "git", "status": "ok", "detail": "good"},
                {"name": "gh", "status": "error", "detail": "missing"},
            ],
        }
    )

    assert dialog["title"] == "Doctor"
    assert dialog["overall"] == "warning"
    assert dialog["checks"] == [
        {"name": "git", "status": "ok", "detail": "good"},
        {"name": "gh", "status": "error", "detail": "missing"},
    ]


def test_build_session_diagnostics_dialog_actions():
    dialog = build_session_diagnostics_dialog(
        42,
        {
            "manifest": {
                "session_name": "sess-1",
                "started_at": "2024-01-01",
                "run_id": "run-1",
                "backend": "tmux",
                "agent_label": "codex",
                "claude_session_id": "cl-1",
                "worktree": "/wt",
                "diagnostic_path": "diag/diagnostic.json",
                "claude_log_path": "/logs/claude.log",
                "claude_log_dir": "/logs",
                "orchestrator_log": "/logs/orch.log",
                "validation_record_path": "validate.json",
            },
            "run_dir": "/run/dir",
            "session_name": "fallback",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Session"] == "sess-1"
    assert rows["Worktree"] == "/wt"

    action_types = [action["type"] for action in dialog["actions"]]
    assert "open_path" in action_types
    assert "open_agent_log" in action_types
    assert "view_claude_log" in action_types
    assert "open_orchestrator_log" in action_types
    agent_log_action = next(action for action in dialog["actions"] if action["type"] == "open_agent_log")
    claude_action = next(action for action in dialog["actions"] if action["type"] == "view_claude_log")
    orchestrator_action = next(action for action in dialog["actions"] if action["type"] == "open_orchestrator_log")
    assert agent_log_action["run_dir"] == "/run/dir"
    assert claude_action["run_dir"] == "/run/dir"
    assert orchestrator_action["run_dir"] == "/run/dir"

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/run/dir" in paths
    assert "/logs/claude.log" in paths
    assert "/logs" in paths
    assert "/logs/orch.log" in paths
    assert "/wt/diag/diagnostic.json" in paths
    assert "/wt/validate.json" in paths


def test_build_session_diagnostics_dialog_fallbacks_without_worktree():
    dialog = build_session_diagnostics_dialog(
        7,
        {
            "manifest": {
                "session_name": "",
                "validation_record_path": "validate.json",
            },
            "run_dir": "/run/fallback",
            "session_name": "fallback-session",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Session"] == "fallback-session"
    assert rows["Worktree"] == "-"
    agent_log_action = next(action for action in dialog["actions"] if action["type"] == "open_agent_log")
    orchestrator_action = next(action for action in dialog["actions"] if action["type"] == "open_orchestrator_log")
    assert agent_log_action["run_dir"] == "/run/fallback"
    assert orchestrator_action["run_dir"] == "/run/fallback"
    assert all(action["type"] != "view_claude_log" for action in dialog["actions"])

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/run/fallback" in paths
    # No worktree means relative validation path cannot be resolved/opened.
    assert "validate.json" not in paths


def test_build_blocked_issues_dialog():
    dialog = build_blocked_issues_dialog({"blocked_issues": ["M1-1"]})

    assert dialog == {
        "title": "Blocked Issues",
        "blocked_issues": ["M1-1"],
    }


def test_build_phase_dialog_in_progress():
    dialog = build_phase_dialog(
        {
            "phases": [
                {"name": "coding-1", "display_name": "Coding 1"},
                {"name": "review-1", "display_name": "Review 1"},
                {"name": "coding-2", "display_name": "Coding 2"},
            ]
        },
        issue_number=7,
        phase_key="in_progress",
    )

    assert dialog["title"] == "Coding 2"
    assert dialog["phase"]["name"] == "coding-2"


def test_build_phase_dialog_review_and_default():
    phases_payload = {
        "phases": [
            {"name": "coding-1", "display_name": "Coding 1"},
            {"name": "review-1", "display_name": "Review 1"},
        ]
    }

    review_dialog = build_phase_dialog(phases_payload, issue_number=9, phase_key="review")
    assert review_dialog["title"] == "Review 1"
    assert review_dialog["phase"]["name"] == "review-1"

    default_dialog = build_phase_dialog(phases_payload, issue_number=9, phase_key=None)
    assert default_dialog["phase"]["name"] == "review-1"


def test_build_phase_dialog_specific_match():
    dialog = build_phase_dialog(
        {"phases": [{"name": "triage", "display_name": "Triage"}]},
        issue_number=1,
        phase_key="triage",
    )

    assert dialog["title"] == "Triage"
    assert dialog["phase"]["name"] == "triage"
