"""AI provider API key management via system keyring."""

import os
from typing import Optional

KEYRING_SERVICE = "issue-orchestrator"

# Known AI providers with setup instructions
AI_PROVIDERS = {
    "ANTHROPIC_API_KEY": {
        "name": "Claude (Anthropic)",
        "setup_cmd": "claude setup-token",  # Run this in terminal
        "setup_help": "Run 'claude setup-token' in another terminal, then paste the key here",
        "url": "https://console.anthropic.com/settings/keys",  # Fallback if CLI not available
    },
    "OPENAI_API_KEY": {
        "name": "OpenAI (Codex/GPT)",
        "setup_cmd": None,  # No CLI setup
        "setup_help": "Get your key at: https://platform.openai.com/api-keys",
        "url": "https://platform.openai.com/api-keys",
    },
    "GOOGLE_API_KEY": {
        "name": "Gemini (Google)",
        "setup_cmd": None,
        "setup_help": "Get your key at: https://makersuite.google.com/app/apikey",
        "url": "https://makersuite.google.com/app/apikey",
    },
}


def read_ai_key(key_name: str) -> Optional[str]:
    """Read AI API key from keyring, fall back to env var.

    Args:
        key_name: The environment variable name (e.g., ANTHROPIC_API_KEY)

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
        key_name: The environment variable name (e.g., ANTHROPIC_API_KEY)
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


def list_ai_keys() -> dict[str, tuple[Optional[str], str]]:
    """List all known AI keys and their status.

    Returns:
        Dict mapping key_name to (masked_value, source) where:
        - masked_value is the masked key or None if not set
        - source is 'keyring', 'env', or 'not set'
    """
    result = {}
    for key_name in AI_PROVIDERS:
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
    for key_name in AI_PROVIDERS:
        value = read_ai_key(key_name)
        if value:
            env[key_name] = value
    return env


def _mask_key(value: str) -> str:
    """Mask an API key for display.

    Shows first 6 and last 4 characters for keys longer than 14 chars.
    """
    if len(value) > 14:
        return value[:6] + "..." + value[-4:]
    return "***"
