from issue_orchestrator._vendor.agent_runner.runner import _format_command_for_log


def test_format_command_for_log_keeps_flags_visible() -> None:
    command = [
        "claude",
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "short prompt",
    ]
    rendered = _format_command_for_log(command)
    assert "--output-format stream-json" in rendered
    assert "--include-partial-messages" in rendered


def test_format_command_for_log_truncates_long_arguments() -> None:
    long_prompt = "x" * 300
    rendered = _format_command_for_log(["claude", "--append-system-prompt", long_prompt], max_arg_length=40)
    assert "--append-system-prompt" in rendered
    assert "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx..." in rendered
