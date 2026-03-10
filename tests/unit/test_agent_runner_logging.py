from issue_orchestrator.execution.agent_runner_types import _format_command_for_log


def test_format_command_for_log_keeps_flags_visible() -> None:
    command = [
        "claude",
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        "some system prompt",
    ]
    rendered = _format_command_for_log(command)
    assert "--permission-mode bypassPermissions" in rendered
    assert "--append-system-prompt" in rendered


def test_format_command_for_log_truncates_long_arguments() -> None:
    long_prompt = "x" * 300
    rendered = _format_command_for_log(["claude", "--append-system-prompt", long_prompt], max_arg_length=40)
    assert "--append-system-prompt" in rendered
    assert "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx..." in rendered
