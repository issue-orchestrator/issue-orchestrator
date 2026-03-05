"""Template variable validation."""

import re
from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class TemplateValidator(ConfigValidator):
    """Validates template variables in agent configs.

    Checks:
    - initial_prompt only uses valid template variables
    - command only uses valid template variables
    """

    # Valid variables for initial_prompt (before command rendering)
    VALID_INITIAL_PROMPT_VARS = {
        "issue_number",
        "issue_title",
        "prompt",
        "worktree",
        "model",
        "permission_mode",
        "claude_args",
        "pr_number",  # Only valid for review agents, but we allow it here
    }

    # Valid variables for command (after initial_prompt is rendered)
    # system_prompt includes completion command instructions, built by get_command()
    VALID_COMMAND_VARS = VALID_INITIAL_PROMPT_VARS | {"initial_prompt", "system_prompt"}

    # Regex to find {variable_name} patterns (excluding {{ escaped braces }})
    VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def validate(self, config: "Config") -> list[str]:
        errors = []

        for label, agent in config.agents.items():
            # Check initial_prompt
            found_vars = set(self.VAR_PATTERN.findall(agent.initial_prompt))
            bad_vars = found_vars - self.VALID_INITIAL_PROMPT_VARS
            if bad_vars:
                vars_str = ", ".join(sorted(bad_vars))
                errors.append(
                    f"Agent '{label}': invalid template variable(s) in initial_prompt: {{{vars_str}}}. "
                    f"Valid: issue_number, issue_title, prompt, worktree, model, permission_mode, "
                    f"claude_args, pr_number"
                )

            # Check command
            found_vars = set(self.VAR_PATTERN.findall(agent.command))
            bad_vars = found_vars - self.VALID_COMMAND_VARS
            if bad_vars:
                vars_str = ", ".join(sorted(bad_vars))
                errors.append(
                    f"Agent '{label}': invalid template variable(s) in command: {{{vars_str}}}. "
                    f"Valid: issue_number, issue_title, prompt, worktree, model, permission_mode, "
                    f"claude_args, pr_number, initial_prompt, system_prompt"
                )

        return errors
