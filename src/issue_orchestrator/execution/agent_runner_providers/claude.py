"""Claude Code provider implementation.

Builds command-line invocations for Anthropic's Claude Code CLI.

Previously in ``_vendor/agent_runner/providers/claude.py``.
"""

import json
from typing import TYPE_CHECKING

from issue_orchestrator.domain.provider_lane import BillingMode
from issue_orchestrator.ports.provider_readiness import (
    ProviderEntitlement,
    ProviderReadiness,
)
from issue_orchestrator.ports.provider_resilience import ProviderErrorType

from .base import CLIProvider

if TYPE_CHECKING:
    from pathlib import Path

    from issue_orchestrator.domain.sandbox_scope import SandboxScope
    from issue_orchestrator.ports.command_runner import CommandRunner


class ClaudeCodeProvider(CLIProvider):
    """Provider for Anthropic's Claude Code CLI.

    Runs Claude Code as an interactive TUI session. The initial prompt
    is passed as a positional argument (not ``-p``), which starts the TUI
    and immediately begins working while still showing the full interactive
    output. Follow-up prompts can be delivered via PTY stdin.
    """

    # Model name mappings (short names to full IDs if needed)
    MODEL_ALIASES: dict[str, str] = {
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
        "fable": "fable",
    }
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    # Fable bills against its own weekly meter on subscription plans, separate
    # from the Opus/Sonnet/Haiku pool. Without this the circuit breaker treats
    # an exhausted Fable meter as "claude-code is down" and stops Opus agents
    # whose own capacity is untouched.
    QUOTA_METERS: dict[str, str] = {"fable": "fable"}

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def executable(self) -> str:
        return "claude"

    @property
    def description(self) -> str:
        return "Anthropic Claude Code CLI"

    @property
    def interactive(self) -> bool:
        return True

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        working_directory: "Path | None" = None,
        **kwargs: str,
    ) -> list[str]:
        """Build a Claude Code CLI command for interactive mode.

        The prompt is passed as a positional argument (without ``-p``),
        which starts the interactive TUI and immediately begins working.

        Args:
            prompt: The task to perform (passed as positional arg)
            model: Model name (haiku, sonnet, opus, or full model ID). None for default.
            sandbox_scope: When set, replaces the default ``bypassPermissions``
                (yolo) launch with a bounded OS sandbox — ``--permission-mode
                dontAsk`` plus inline ``--settings`` describing the read/write
                roots, egress, and denied credentials. ``None`` (default) keeps
                the existing command byte-for-byte.
            **kwargs: Additional options:
                - permission_mode: Permission handling mode (default: bypassPermissions).
                  Ignored when ``sandbox_scope`` is set (``dontAsk`` is forced).
                - effort: Claude effort level (low, medium, high, xhigh, max)
                - reasoning_effort: Alias for effort
                - system_prompt: Additional system prompt text
                - max_turns: Maximum conversation turns
        """
        cmd = [self.executable]

        # Model (optional - Claude will use default if not specified)
        if model:
            resolved_model = self.MODEL_ALIASES.get(model, model)
            cmd.extend(["--model", resolved_model])

        effort = self._resolve_effort(kwargs)
        if effort:
            cmd.extend(["--effort", effort])

        if sandbox_scope is not None:
            # Bounded OS sandbox: dontAsk + inline --settings. Replaces the
            # default bypassPermissions (yolo) permission-mode flag.
            cmd.extend(self.apply_scope(sandbox_scope))
        else:
            # Permission mode (default to bypassPermissions for automation)
            permission_mode = kwargs.get("permission_mode", "bypassPermissions")
            cmd.extend(["--permission-mode", permission_mode])

        # Optional system prompt
        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Optional max turns
        max_turns = kwargs.get("max_turns")
        if max_turns:
            cmd.extend(["--max-turns", str(max_turns)])

        # Disable MCP servers — worktree .mcp.json can contain configs
        # (e.g. Playwright) that hang in automated/headless contexts.
        cmd.extend(["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"])

        # Verbose mode (more detailed TUI output)
        verbose = kwargs.get("verbose")
        if verbose and str(verbose).lower() not in ("false", "0", "no", ""):
            cmd.append("--verbose")

        # Initial prompt as positional argument — starts TUI working immediately
        # without -p flag, so full interactive output is preserved.
        if prompt:
            cmd.append(prompt)

        return cmd

    # ``claude auth status --json`` is the cheapest reliable credential probe:
    # it reads local credential state only (no API round-trip, no tokens spent)
    # and answers in well under a second, which is what makes it affordable on
    # every launch. Its ``loggedIn`` field is the authoritative signal — the
    # expired-login TUI banner (#6999) is the *symptom* this probe predicts.
    AUTH_STATUS_ARGV = ("auth", "status", "--json")

    def check_readiness(self, runner: "CommandRunner") -> ProviderReadiness:
        """Probe Claude Code's local credential state without spawning a TUI."""
        if not self.is_available():
            return ProviderReadiness.not_installed(
                self.name, f"{self.executable} not found in PATH"
            )
        output, exit_code, timed_out = self._run_auth_probe(
            runner, [self.executable, *self.AUTH_STATUS_ARGV]
        )
        if timed_out:
            return ProviderReadiness.unknown(
                self.name,
                f"`{self.executable} auth status` timed out after "
                f"{self.AUTH_PROBE_TIMEOUT_SECONDS}s",
            )
        return self._interpret_auth_status(output, exit_code)

    def _interpret_auth_status(
        self, output: str, exit_code: int | None
    ) -> ProviderReadiness:
        """Turn one ``auth status`` result into a typed readiness.

        All raw interpretation of Claude's output lives here; token matching is
        delegated to the shared classification table so no second table exists.
        """
        logged_in = self._logged_in_flag(output)
        # Billing comes from the SAME probe execution as the login verdict. It
        # is attached to every outcome, not just the successful one: an expired
        # credential still tells you which kind of account it belonged to, and
        # the circuit needs that to decide whether the lane heals on a timer.
        entitlement = self.read_entitlement(output, exit_code)
        if logged_in is True:
            return ProviderReadiness.ready(
                self.name,
                f"{self.executable} auth status: logged in",
                entitlement,
            )
        if logged_in is False:
            return ProviderReadiness.auth_expired(
                self.name,
                f"{self.executable} auth status reports not logged in — "
                "run `claude /login`",
                entitlement,
            )
        if self.classify_output(output) is ProviderErrorType.AUTH:
            return ProviderReadiness.auth_expired(
                self.name,
                f"{self.executable} auth status reported an auth failure — "
                "run `claude /login`",
                entitlement,
            )
        return ProviderReadiness.unknown(
            self.name,
            f"`{self.executable} auth status` gave no verdict (exit={exit_code})",
            entitlement,
        )

    #: ``apiProvider`` values that mean "billed per token against a cloud
    #: account" rather than against a Claude subscription.
    _METERED_API_PROVIDERS = frozenset({"bedrock", "vertex", "apikey", "console"})
    #: ``authMethod`` substrings that mean the same thing.
    _METERED_AUTH_MARKERS = ("apikey", "api_key", "api key", "bedrock", "vertex")

    def read_entitlement(
        self, output: str, exit_code: int | None
    ) -> ProviderEntitlement:
        """Read how this Claude account pays, from the probe already running.

        ``claude auth status --json`` reports ``authMethod``, ``apiProvider``
        and ``subscriptionType`` alongside ``loggedIn``. Before #7253 only
        ``loggedIn`` was read and the rest was discarded, so the orchestrator
        could not tell a Max subscription (prepaid, self-refilling weekly) from
        an API key (metered, real money per token) — a distinction that decides
        whether an exhausted lane may reopen on its own.

        Only a positive subscription signal yields ``PREPAID``. Anything
        unrecognised stays undetermined and therefore resolves to metered: this
        is the direction that cannot spend an operator's money by mistake.
        """
        del exit_code  # the JSON body carries the verdict
        payload = self._auth_payload(output)
        if payload is None:
            return ProviderEntitlement()
        auth_method = str(payload.get("authMethod", "") or "")
        api_provider = str(payload.get("apiProvider", "") or "")
        plan = str(payload.get("subscriptionType", "") or "")
        normalized_auth = auth_method.strip().lower()
        normalized_api = api_provider.strip().lower()

        billing: BillingMode | None = None
        if normalized_api in self._METERED_API_PROVIDERS or any(
            marker in normalized_auth for marker in self._METERED_AUTH_MARKERS
        ):
            billing = BillingMode.METERED
        elif plan.strip():
            # A subscription tier is the only positive evidence of prepaid
            # capacity. `apiProvider: firstParty` alone is not enough — it says
            # the request goes to Anthropic, not how it is paid for.
            billing = BillingMode.PREPAID
        return ProviderEntitlement(
            billing=billing,
            auth_method=auth_method,
            plan=plan,
        )

    @classmethod
    def _logged_in_flag(cls, output: str) -> bool | None:
        """Read ``loggedIn`` out of the probe's JSON, or ``None`` if absent."""
        payload = cls._auth_payload(output)
        if payload is None or "loggedIn" not in payload:
            return None
        return bool(payload["loggedIn"])

    @staticmethod
    def _auth_payload(output: str) -> dict | None:
        """Extract the probe's JSON object, or ``None`` if it is not there.

        The CLI may prefix diagnostics before the JSON document, so the object
        is extracted by brace span rather than assuming the whole stream parses.
        """
        start = output.find("{")
        end = output.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(output[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def apply_scope(self, scope: "SandboxScope") -> list[str]:
        """Translate a :class:`SandboxScope` into claude-code sandbox argv.

        Under the trusted-repository contract (ADR-0034) the target repo's
        configuration is trusted policy, so no settings-source guard is needed:
        we translate the scope directly into Claude's native OS sandbox
        (``--permission-mode dontAsk`` + inline ``--settings``). The boundary the
        adapter still enforces is against the *agent* — worktree-scoped writes,
        denied secrets/egress, and denied self-modification of the policy files.
        """
        from .sandbox import ClaudeSandboxAdapter

        return ClaudeSandboxAdapter().apply_scope(scope)

    @classmethod
    def _resolve_effort(cls, kwargs: dict[str, str]) -> str | None:
        effort = cls._normalize_effort(kwargs.get("effort"))
        reasoning_effort = cls._normalize_effort(kwargs.get("reasoning_effort"))
        if effort and reasoning_effort and effort != reasoning_effort:
            raise ValueError(
                "Claude effort and reasoning_effort must match when both are set"
            )
        normalized = effort or reasoning_effort
        if normalized is None:
            return None
        if normalized not in cls.EFFORT_LEVELS:
            allowed = ", ".join(cls.EFFORT_LEVELS)
            raise ValueError(
                f"Claude effort must be one of {allowed}; got {normalized!r}"
            )
        return normalized

    @staticmethod
    def _normalize_effort(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None
