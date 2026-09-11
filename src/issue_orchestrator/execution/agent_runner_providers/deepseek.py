"""DeepSeek provider: the Claude Code CLI pointed at DeepSeek's API (#7253).

DeepSeek ships no first-party coding CLI. Every "DeepSeek agent" — Deep Code,
DeepSeek-TUI, Reasonix — is a third-party project, and DeepSeek's own
integration guide lists Claude Code first among supported harnesses because it
publishes an Anthropic-compatible endpoint for exactly this purpose.

So this provider adds no new process stack. It is the ``claude`` CLI with three
things changed — base URL, credential, and model — which is why it subclasses
:class:`ClaudeCodeProvider` instead of reimplementing PTY handling, sandbox
translation, hook wiring and session-log parsing that already work.

It is nonetheless a *distinct provider*, not a model option, because:

* **Circuit identity.** A DeepSeek rate limit must not open the circuit on
  Anthropic sessions, and vice versa. The breaker keys on provider/lane.
* **Readiness.** ``claude auth status`` reports on the *Anthropic* login, which
  says nothing about whether a DeepSeek key exists. Inheriting that probe would
  cheerfully answer READY with no DeepSeek credential at all.
* **Billing.** DeepSeek sells no subscription. It is always metered, which
  changes how an exhausted lane heals.

``ai_system`` stays ``claude-code``: the session writes the same
``~/.claude/projects/**.jsonl``, so log discovery, console-tag detection and the
``coding-done`` completion marker are already correct for it.
"""

from typing import TYPE_CHECKING

from issue_orchestrator.domain.provider_lane import BillingMode
from issue_orchestrator.ports.provider_readiness import (
    ProviderEntitlement,
    ProviderReadiness,
)

from .claude import ClaudeCodeProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from issue_orchestrator.ports.command_runner import CommandRunner


class DeepSeekProvider(ClaudeCodeProvider):
    """Provider for DeepSeek models served through the Claude Code CLI."""

    #: DeepSeek's Anthropic-compatible endpoint.
    BASE_URL = "https://api.deepseek.com/anthropic"
    #: The keyring/env name holding the DeepSeek credential.
    API_KEY_NAME = "DEEPSEEK_API_KEY"

    #: DeepSeek model slugs. Deliberately replaces Claude's aliases rather than
    #: extending them: ``--model opus`` against DeepSeek's endpoint is a
    #: configuration mistake, and mapping it silently would hide that.
    MODEL_ALIASES: dict[str, str] = {
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-flash": "deepseek-flash",
    }

    #: Metered billing buys one pool of currency, so there is nothing to split.
    #: Declared explicitly to override the Fable meter inherited from Claude —
    #: ``deepseek + fable`` is not a lane, it is a typo.
    QUOTA_METERS: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def description(self) -> str:
        return "DeepSeek via the Claude Code CLI"

    @property
    def required_secret_names(self) -> tuple[str, ...]:
        return (self.API_KEY_NAME,)

    def session_env(self, *, secrets: "Mapping[str, str]") -> dict[str, str]:
        """Point the CLI at DeepSeek and hand it the credential.

        Returned as environment, never as argv: ``ps`` is world-readable on a
        shared machine, so an ``env DEEPSEEK_API_KEY=...`` command prefix would
        publish the key to every local user. ``AgentSpec.env_overrides`` puts it
        in the process environment instead.

        ``ANTHROPIC_API_KEY`` is the variable DeepSeek's own integration guide
        documents for this endpoint.
        """
        env = {"ANTHROPIC_BASE_URL": self.BASE_URL}
        key = secrets.get(self.API_KEY_NAME)
        if key:
            env["ANTHROPIC_API_KEY"] = key
        return env

    def read_entitlement(
        self, output: str, exit_code: int | None
    ) -> ProviderEntitlement:
        """DeepSeek is pay-as-you-go; there is no subscription to detect."""
        del output, exit_code  # billing is a property of the product, not the probe
        return ProviderEntitlement(
            billing=BillingMode.METERED, auth_method=self.API_KEY_NAME
        )

    def check_readiness(self, runner: "CommandRunner") -> ProviderReadiness:
        """Confirm the CLI is installed and a DeepSeek credential exists.

        Deliberately does **not** call ``claude auth status``: that reports on
        the operator's Anthropic login, which is irrelevant — and worse than
        irrelevant, because a healthy Anthropic subscription would report READY
        while DeepSeek sessions failed on a missing key.

        Like the Claude and Codex probes, this reads local credential state
        only — no API round-trip, no tokens spent, nothing billed. A key that is
        present but revoked or out of balance surfaces as a classified quota or
        auth failure at run time, which is the same standard the other
        providers are held to.
        """
        del runner  # credential state is local; nothing needs executing
        if not self.is_available():
            return ProviderReadiness.not_installed(
                self.name,
                f"{self.executable} not found in PATH (DeepSeek runs through "
                "the Claude Code CLI)",
            )
        entitlement = self.read_entitlement("", None)
        if not self._credential_present():
            return ProviderReadiness.auth_expired(
                self.name,
                f"no {self.API_KEY_NAME} found — run "
                f"`issue-orchestrator keys set deepseek`",
                entitlement,
            )
        return ProviderReadiness.ready(
            self.name,
            f"{self.API_KEY_NAME} available",
            entitlement,
        )

    def _credential_present(self) -> bool:
        """Whether a DeepSeek key is reachable from the keyring or environment."""
        from issue_orchestrator.infra.ai_keys import read_ai_key

        return bool(read_ai_key(self.API_KEY_NAME))
