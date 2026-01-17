"""Agent configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class AgentValidator(ConfigValidator):
    """Validates agent-related configuration.

    Checks:
    - At least one agent is configured
    - Prompt files exist
    - Providers are valid
    - Models are known (for claude-code provider)
    - Per-agent reviewers reference valid agents
    - Default agent provider is valid
    """

    KNOWN_CLAUDE_MODELS = {"haiku", "sonnet", "opus"}

    def validate(self, config: "Config") -> list[str]:
        from agent_runner import is_valid_provider, list_providers

        errors = []

        # Must have at least one agent
        if not config.agents:
            errors.append(
                "No agents configured. Add at least one agent under 'agents:' in config."
            )

        # Validate default_agent.provider if set
        if config.default_agent and config.default_agent.provider is not None:
            if not is_valid_provider(config.default_agent.provider):
                errors.append(
                    f"default_agent.provider '{config.default_agent.provider}' is not valid. "
                    f"Available: {list_providers()}"
                )

        for label, agent in config.agents.items():
            errors.extend(self._validate_agent(config, label, agent, list_providers, is_valid_provider))

        return errors

    def _validate_agent(
        self,
        config: "Config",
        label: str,
        agent,  # AgentConfig - can't use type hint due to nested class
        list_providers,
        is_valid_provider,
    ) -> list[str]:
        """Validate a single agent configuration."""
        errors = []

        # Prompt file must exist
        if not agent.prompt_path.exists():
            errors.append(
                f"Agent '{label}': prompt file not found: {agent.prompt_path}"
            )

        # Provider must be set (from agent or default_agent) or command overridden
        has_custom_command = "command" in config.raw_agents.get(label, {})
        if agent.provider is None and not has_custom_command:
            errors.append(
                f"Agent '{label}': no provider specified and no default_agent.provider set. "
                f"Either set 'provider' on the agent, set 'default_agent.provider', "
                f"or use 'command' to specify a custom command. "
                f"Available providers: {list_providers()}"
            )
        elif agent.provider is not None and not is_valid_provider(agent.provider):
            errors.append(
                f"Agent '{label}': unknown provider '{agent.provider}'. "
                f"Available: {list_providers()}"
            )

        # Model validation for claude-code provider
        if agent.provider in (None, "claude-code") and agent.model not in self.KNOWN_CLAUDE_MODELS:
            errors.append(
                f"Agent '{label}': unknown model '{agent.model}'. Known: {self.KNOWN_CLAUDE_MODELS}"
            )

        # Per-agent reviewer must reference valid agent
        if agent.reviewer and agent.reviewer not in config.agents:
            errors.append(
                f"Agent '{label}': reviewer '{agent.reviewer}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

        return errors
