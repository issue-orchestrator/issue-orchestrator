"""What a launched agent is given to read (#9012).

A launch command carries no prompt text, only the instruction to read the run's
prompt file. Tests that ask "what was the agent told?" read that file, whole, as
the agent is instructed to.
"""

from __future__ import annotations

import re
from pathlib import Path

from issue_orchestrator.domain.launch_prompt_delivery import (
    LAUNCH_PROMPT_INSTRUCTION_OPENING,
    LaunchCommand,
    PreparedSessionCommand,
)
from issue_orchestrator.ports.session_runner import NullSessionRunner

_INSTRUCTION = re.compile(
    re.escape(LAUNCH_PROMPT_INSTRUCTION_OPENING) + r"(?P<path>.+?) \((?P<lines>\d+) lines, (?P<characters>\d+) characters\)"
)


def delivered_prompt(command: str) -> str:
    """The prompt *command*'s agent is told to read, checked to be the whole file."""
    instructions = list(_INSTRUCTION.finditer(command))
    assert len(instructions) == 1, f"expected one launch-prompt instruction in: {command[:500]}"
    instruction = instructions[0]
    prompt = Path(instruction["path"]).read_text()
    assert len(prompt) == int(instruction["characters"])
    assert len(prompt.splitlines()) == int(instruction["lines"])
    return prompt


def prepared_as_given(command: LaunchCommand, working_dir: Path) -> PreparedSessionCommand:
    """A terminal double's preparation: the command exactly as given, nothing put around it."""
    return NullSessionRunner().prepare_session(command, working_dir)
