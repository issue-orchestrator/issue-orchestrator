"""AI provider checks for doctor."""

from ..types import Check
from ...config import Config
from ...hooks.hooks import MetaAgentType


# AI systems that authenticate via their own CLI - no API key needed
CLI_ONLY_AI_SYSTEMS: frozenset[MetaAgentType] = frozenset({
    MetaAgentType.CLAUDE_CODE,
    MetaAgentType.CODEX,
    MetaAgentType.GEMINI,
    MetaAgentType.CURSOR,
    MetaAgentType.COPILOT,
    MetaAgentType.AIDER,
})

PROVIDER_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "oai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _infer_ai_system_from_command(command: str | None) -> MetaAgentType | None:
    """Infer AI system type from command executable."""
    if not command:
        return None
    executable = command.strip().split()[0] if command.strip() else ""
    executable = executable.rsplit("/", 1)[-1]
    if executable.startswith("claude"):
        return MetaAgentType.CLAUDE_CODE
    if executable.startswith("codex"):
        return MetaAgentType.CODEX
    if executable.startswith("gemini"):
        return MetaAgentType.GEMINI
    if executable.startswith("cursor"):
        return MetaAgentType.CURSOR
    if executable.startswith("aider"):
        return MetaAgentType.AIDER
    return None


def _is_cli_only_provider(provider: str | None, command: str | None) -> bool:
    """Check if provider/command represents a CLI-only AI system (no API key needed)."""
    # Check explicit provider first
    if provider:
        try:
            ai_system = MetaAgentType(provider.strip().lower())
            return ai_system in CLI_ONLY_AI_SYSTEMS
        except ValueError:
            # Not a known AI system (e.g., "anthropic", "openai") - may need API key
            return False

    # No explicit provider - try to infer from command
    ai_system = _infer_ai_system_from_command(command)
    return ai_system is not None and ai_system in CLI_ONLY_AI_SYSTEMS


def _collect_required_keys(config: Config) -> tuple[set[str], set[str]]:
    required_keys: set[str] = set()
    unknown_providers: set[str] = set()

    default_provider = None
    if config.default_agent:
        default_provider = config.default_agent.provider

    for agent in config.agents.values():
        provider = agent.provider or default_provider
        command = getattr(agent, "command", None)

        if _is_cli_only_provider(provider, command):
            continue

        if not provider:
            continue

        normalized = provider.strip().lower()
        key_name = PROVIDER_KEY_MAP.get(normalized)
        if key_name:
            required_keys.add(key_name)
        else:
            unknown_providers.add(normalized)

    return required_keys, unknown_providers


def check_ai_keys(config: Config) -> list[Check]:
    from ...ai_keys import list_ai_keys, AI_PROVIDERS

    checks: list[Check] = []

    required_keys, unknown_providers = _collect_required_keys(config)
    ai_keys = list_ai_keys()

    if required_keys:
        missing = []
        configured = []
        for key_name in sorted(required_keys):
            _, source = ai_keys.get(key_name, (None, "not set"))
            provider_name = AI_PROVIDERS.get(key_name, {}).get("name", key_name)
            if source == "not set":
                missing.append(provider_name)
            else:
                configured.append(f"{provider_name} ({source})")

        if missing:
            checks.append(Check(
                name="AI Provider Keys",
                status="warning",
                detail="Missing keys for: " + ", ".join(missing),
            ))
        else:
            checks.append(Check(
                name="AI Provider Keys",
                status="ok",
                detail=", ".join(configured),
            ))
    else:
        checks.append(Check(
            name="AI Provider Keys",
            status="info",
            detail="No API keys required for configured providers",
        ))

    if unknown_providers:
        checks.append(Check(
            name="AI Provider Keys (Unknown Providers)",
            status="info",
            detail="No API key check for: " + ", ".join(sorted(unknown_providers)),
        ))

    return checks


def check_ai_provider_clis() -> list[Check]:
    from agent_runner import list_providers, get_provider

    checks: list[Check] = []

    providers = list_providers()
    available_providers = []
    missing_providers = []

    for name in providers:
        provider = get_provider(name)
        if provider.is_available():
            version = provider.check_version()
            version_info = f" ({version})" if version else ""
            available_providers.append(f"{name}{version_info}")
        else:
            missing_providers.append(name)

    if available_providers:
        checks.append(Check(
            name="AI Provider CLIs",
            status="ok",
            detail=", ".join(available_providers),
        ))
    else:
        checks.append(Check(
            name="AI Provider CLIs",
            status="error",
            detail=f"No CLIs installed. Install one of: {', '.join(providers)}",
        ))

    if missing_providers:
        checks.append(Check(
            name="AI Provider CLIs (Missing)",
            status="info",
            detail=f"Not installed: {', '.join(missing_providers)}",
        ))

    return checks
