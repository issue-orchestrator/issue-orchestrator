"""Keyring-backed provider credential resolution (#7253).

The adapter side of :mod:`issue_orchestrator.ports.provider_credentials`. It
asks the provider adapter which secrets it needs, reads exactly those from the
system keyring (falling back to the process environment), and lets the provider
shape them into the environment its CLI expects.

Only what a provider *names* is read. The orchestrator does not hand every
stored key to every session: a Codex agent has no business receiving the
operator's DeepSeek credential just because it happens to be in the keyring.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from .agent_runner_providers import get_provider

logger = logging.getLogger(__name__)

__all__ = ["KeyringProviderCredentials"]


class KeyringProviderCredentials:
    """Resolve a provider's declared secrets from the keyring or environment."""

    def session_env(self, provider: str | None) -> Mapping[str, str]:
        if not provider:
            return {}
        try:
            adapter = get_provider(provider)
        except ValueError:
            # An unknown provider names no secrets. Configuration validation
            # already rejects it; returning empty keeps this off the failure
            # path rather than raising from inside a launch.
            return {}
        required = adapter.required_secret_names
        secrets = self._read_secrets(provider, required)
        return adapter.session_env(secrets=secrets)

    @staticmethod
    def _read_secrets(
        provider: str, names: tuple[str, ...]
    ) -> dict[str, str]:
        if not names:
            return {}
        from issue_orchestrator.infra.ai_keys import read_ai_key

        resolved: dict[str, str] = {}
        for name in names:
            value = read_ai_key(name)
            if value:
                resolved[name] = value
            else:
                # Logged without the value, and at warning level because the
                # session will fail: the provider's readiness probe reports the
                # same missing key with the command that fixes it.
                logger.warning(
                    "[provider] %s needs %s but no value is configured "
                    "(run `issue-orchestrator keys set %s`)",
                    provider,
                    name,
                    provider,
                )
        return resolved
