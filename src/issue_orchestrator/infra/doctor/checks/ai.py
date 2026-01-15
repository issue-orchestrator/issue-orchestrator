"""AI provider checks for doctor."""

from ..types import Check
from ...config import Config


CLI_ONLY_PROVIDERS = {
    "claude-code",
    "codex",
    "codex-cli",
}

PROVIDER_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "oai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _infer_provider_from_command(command: str | None) -> str | None:
    if not command:
        return None
    executable = command.strip().split()[0] if command.strip() else ""
    executable = executable.rsplit("/", 1)[-1]
    if executable.startswith("claude"):
        return "claude-code"
    if executable.startswith("codex"):
        return "codex-cli"
    return None


def _collect_required_keys(config: Config) -> tuple[set[str], set[str]]:
    required_keys: set[str] = set()
    unknown_providers: set[str] = set()

    default_provider = None
    if config.default_agent:
        default_provider = config.default_agent.provider

    for agent in config.agents.values():
        provider = agent.provider or default_provider
        if provider is None:
            provider = _infer_provider_from_command(getattr(agent, "command", None))
        if not provider:
            continue

        normalized = provider.strip().lower()
        if normalized in CLI_ONLY_PROVIDERS:
            continue

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
