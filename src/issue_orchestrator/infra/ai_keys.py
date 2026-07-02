"""AI provider API key management via system keyring."""

from functools import cache
from importlib import metadata
import os
from typing import Any, NotRequired, TypedDict, cast

KEYRING_SERVICE = "issue-orchestrator"
PROVIDER_KEY_ENTRY_POINT_GROUP = "issue_orchestrator.ai_provider_keys"


class ProviderKeyInfo(TypedDict):
    """Metadata for an API key that can be stored in the keyring."""

    name: str
    setup_cmd: NotRequired[str | None]
    setup_help: NotRequired[str]
    url: NotRequired[str]
    provider_aliases: NotRequired[tuple[str, ...] | list[str] | set[str]]


class ProviderKeyPluginError(RuntimeError):
    """Raised when an installed provider-key extension is malformed."""


# Known built-in AI providers with setup instructions. Provider-specific API
# key metadata that is not broadly required should live in an optional package
# exposing the ``issue_orchestrator.ai_provider_keys`` entry point group.
_BUILTIN_AI_PROVIDERS: dict[str, ProviderKeyInfo] = {
    "OPENAI_API_KEY": {
        "name": "OpenAI (Codex/GPT)",
        "setup_cmd": None,  # No CLI setup
        "setup_help": "Get your key at: https://platform.openai.com/api-keys",
        "url": "https://platform.openai.com/api-keys",
        "provider_aliases": ("openai", "oai"),
    },
    "GOOGLE_API_KEY": {
        "name": "Gemini (Google)",
        "setup_cmd": None,
        "setup_help": "Get your key at: https://makersuite.google.com/app/apikey",
        "url": "https://makersuite.google.com/app/apikey",
        "provider_aliases": ("gemini", "google"),
    },
}


@cache
def _load_ai_providers() -> dict[str, ProviderKeyInfo]:
    """Load built-in and extension-provided API key metadata.

    Optional provider packages can contribute key metadata by registering an
    entry point in the ``issue_orchestrator.ai_provider_keys`` group. The entry
    point must load either a mapping or a callable returning a mapping:

        {"PROVIDER_API_KEY": {"name": "Provider", "provider_aliases": ("provider",)}}
    """
    providers = _BUILTIN_AI_PROVIDERS.copy()
    for entry_point in metadata.entry_points().select(group=PROVIDER_KEY_ENTRY_POINT_GROUP):
        try:
            loaded = entry_point.load()
            contribution = loaded() if callable(loaded) else loaded
        except Exception as exc:
            raise ProviderKeyPluginError(
                f"Provider key plugin {entry_point.name!r} failed to load"
            ) from exc
        _merge_provider_contribution(providers, contribution, entry_point.name)
    return providers


def get_ai_providers() -> dict[str, ProviderKeyInfo]:
    """Return built-in and extension-provided API key metadata."""
    return {
        key_name: cast(ProviderKeyInfo, dict(info))
        for key_name, info in _load_ai_providers().items()
    }


def clear_ai_provider_cache() -> None:
    """Clear cached provider metadata after entry-point changes in tests."""
    _load_ai_providers.cache_clear()


def get_provider_key_map() -> dict[str, str]:
    """Return provider aliases mapped to their API-key environment names."""
    provider_key_map: dict[str, str] = {}
    for key_name, info in get_ai_providers().items():
        aliases = info.get("provider_aliases", ())
        if not isinstance(aliases, (tuple, list, set)):
            raise ProviderKeyPluginError(
                f"Provider key metadata for {key_name} has invalid provider_aliases"
            )
        for alias in aliases:
            if not isinstance(alias, str):
                raise ProviderKeyPluginError(
                    f"Provider key metadata for {key_name} has a non-string alias"
                )
            normalized = alias.strip().lower()
            if not normalized:
                continue
            existing = provider_key_map.get(normalized)
            if existing and existing != key_name:
                raise ProviderKeyPluginError(
                    f"Provider alias {normalized!r} maps to both {existing} and {key_name}"
                )
            provider_key_map[normalized] = key_name
    return provider_key_map


def normalize_ai_key_name(key_name: str) -> str:
    """Normalize user-provided key names for keyring commands."""
    stripped = key_name.strip()
    if not stripped:
        return ""

    alias_match = get_provider_key_map().get(stripped.lower())
    if alias_match:
        return alias_match

    normalized = stripped.upper()
    providers = get_ai_providers()
    if normalized in providers or normalized.endswith("_API_KEY"):
        return normalized
    return f"{normalized}_API_KEY"


def read_ai_key(key_name: str) -> str | None:
    """Read AI API key from keyring, fall back to env var.

    Args:
        key_name: The environment variable name (e.g., OPENAI_API_KEY)

    Returns:
        The API key value, or None if not found
    """
    # Check keyring first
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, key_name)
        if value:
            return value
    except ImportError:
        pass  # keyring not installed
    except Exception:
        pass  # keyring failed (no backend, locked, etc.)

    # Fall back to environment variable
    return os.environ.get(key_name)


def store_ai_key(key_name: str, value: str) -> None:
    """Store AI API key in system keyring.

    Args:
        key_name: The environment variable name (e.g., OPENAI_API_KEY)
        value: The API key value

    Raises:
        ImportError: If keyring library is not installed
        Exception: If keyring storage fails
    """
    import keyring

    keyring.set_password(KEYRING_SERVICE, key_name, value)


def delete_ai_key(key_name: str) -> bool:
    """Delete AI API key from keyring.

    Args:
        key_name: The environment variable name to delete

    Returns:
        True if deleted, False if not found or failed
    """
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, key_name)
        return True
    except ImportError:
        return False
    except Exception:
        # PasswordDeleteError or other failures
        return False


def list_ai_keys() -> dict[str, tuple[str | None, str]]:
    """List all known AI keys and their status.

    Returns:
        Dict mapping key_name to (masked_value, source) where:
        - masked_value is the masked key or None if not set
        - source is 'keyring', 'env', or 'not set'
    """
    result: dict[str, tuple[str | None, str]] = {}
    for key_name in get_ai_providers():
        # Check keyring first
        try:
            import keyring

            value = keyring.get_password(KEYRING_SERVICE, key_name)
            if value:
                masked = _mask_key(value)
                result[key_name] = (masked, "keyring")
                continue
        except ImportError:
            pass
        except Exception:
            pass

        # Check env var
        value = os.environ.get(key_name)
        if value:
            masked = _mask_key(value)
            result[key_name] = (masked, "env")
        else:
            result[key_name] = (None, "not set")

    return result


def get_ai_keys_for_env() -> dict[str, str]:
    """Get all available AI keys as env vars dict (for session launch).

    Returns:
        Dict mapping env var name to value for all configured AI keys
    """
    env = {}
    for key_name in get_ai_providers():
        value = read_ai_key(key_name)
        if value:
            env[key_name] = value
    return env


def _merge_provider_contribution(
    providers: dict[str, ProviderKeyInfo],
    contribution: Any,
    plugin_name: str,
) -> None:
    if not isinstance(contribution, dict):
        raise ProviderKeyPluginError(
            f"Provider key plugin {plugin_name!r} must return a dict"
        )
    for key_name, info in contribution.items():
        if not isinstance(key_name, str) or not key_name:
            raise ProviderKeyPluginError(
                f"Provider key plugin {plugin_name!r} returned an invalid key name"
            )
        if not isinstance(info, dict):
            raise ProviderKeyPluginError(
                f"Provider key plugin {plugin_name!r} metadata for {key_name} must be a dict"
            )
        if not isinstance(info.get("name"), str) or not info["name"]:
            raise ProviderKeyPluginError(
                f"Provider key plugin {plugin_name!r} metadata for {key_name} needs a name"
            )
        providers[key_name] = cast(ProviderKeyInfo, dict(info))


def _mask_key(value: str) -> str:
    """Mask an API key for display.

    Shows first 6 and last 4 characters for keys longer than 14 chars.
    """
    if len(value) > 14:
        return value[:6] + "..." + value[-4:]
    return "***"
