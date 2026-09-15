"""The one surface control asks for a provider's session credentials (#7253).

Some providers authenticate through their own CLI login and need nothing from
the orchestrator — ``claude`` and ``codex`` read credentials the operator
established with ``claude /login`` / ``codex login``. A provider reached through
a third-party endpoint has no such login: DeepSeek is the Claude Code CLI
pointed at ``api.deepseek.com``, and the orchestrator must hand it a key.

That key must never reach argv. ``ps`` is world-readable, so an
``env DEEPSEEK_API_KEY=... claude ...`` prefix would publish it to every local
user on a shared machine. Values returned here travel in
``AgentSpec.env_overrides``, which lands them in the session's process
environment instead.

Control depends on this port rather than on the provider registry, which lives
in ``execution`` and is therefore off-limits to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

__all__ = [
    "NO_PROVIDER_CREDENTIALS",
    "ProviderCredentials",
    "StaticProviderCredentials",
]


class ProviderCredentials(Protocol):
    """Resolve the secret environment one provider needs to launch."""

    def session_env(self, provider: str | None) -> Mapping[str, str]:
        """Return the environment ``provider`` needs inside its session.

        Empty for a provider that carries its own login, and empty for a
        provider whose declared secret is not configured — the launch then
        fails on the provider's own readiness probe, which names the missing
        key, rather than here with a partial environment.
        """
        ...


class StaticProviderCredentials:
    """Resolves nothing, explicitly.

    Not a fallback: it exists so compositions with no provider registry wired —
    tests, and paths built before the adapter exists — have to name that fact
    rather than silently receive an empty mapping from a real resolver. Same
    pattern as :data:`~.provider_readiness.NO_PROVIDER_READINESS_PROBE`.
    """

    def session_env(self, provider: str | None) -> Mapping[str, str]:
        del provider  # nothing is resolved without a provider registry
        return {}


#: The explicit "no provider credential resolver is wired" value.
NO_PROVIDER_CREDENTIALS: StaticProviderCredentials = StaticProviderCredentials()
