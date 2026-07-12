"""Review workflow configuration validator."""

from typing import TYPE_CHECKING

from .base import ConfigValidator

if TYPE_CHECKING:
    from ..config import Config


class ReviewWorkflowValidator(ConfigValidator):
    """Validates review workflow configuration.

    Checks:
    - If reviews enabled, default reviewer must be set
    - Default reviewer must exist in agents
    - Triage review agent must exist in agents (if set)
    - Triage authority modes are valid; act-level 'execute' rejected (#6764)
    - Triage health-review interval is non-negative (0 = disabled, #6763)
    - A positive health-review interval requires a triage agent (#6776)
    """

    def validate(self, config: "Config") -> list[str]:
        errors: list[str] = []

        self._validate_review_defaults(config, errors)
        self._validate_triage_agent(config, errors)
        self._validate_triage_follow_up_agent(config, errors)
        # Graduated triage authority (ADR-0031): act-level 'execute' is a
        # startup configuration error until its executor is wired (#6764).
        errors.extend(config.triage.authority.startup_errors())
        # Periodic health review (ADR-0031 §4): a negative interval is a
        # startup configuration error, never silently treated as disabled.
        errors.extend(config.triage.health_review.startup_errors())
        self._validate_health_review_requires_agent(config, errors)

        exchange_mode = config.review_exchange_mode
        self._validate_exchange_mode(exchange_mode, config, errors)
        self._validate_probe_schedule(config, errors)
        # Pair validation is deferred to runtime when the actual coder agent is known.

        return errors

    def _validate_review_defaults(self, config: "Config", errors: list[str]) -> None:
        if not config.review_enabled:
            return
        if not config.code_review_agent:
            errors.append(
                "review.enabled is true but no default reviewer set. "
                "Add 'review: default: agent:reviewer' to config."
            )
            return
        if config.code_review_agent not in config.agents:
            errors.append(
                f"review.default '{config.code_review_agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

    def _validate_triage_agent(self, config: "Config", errors: list[str]) -> None:
        if not config.triage_review_agent:
            return
        if config.triage_review_agent not in config.agents:
            errors.append(
                f"triage_review_agent '{config.triage_review_agent}' not found in agents. "
                f"Available: {list(config.agents.keys())}"
            )

    def _validate_triage_follow_up_agent(
        self, config: "Config", errors: list[str]
    ) -> None:
        # Typed destination for triage create_issue proposals (#6779 R9): if
        # set it MUST name a real agent, so routing can never fall back to
        # dict order and hand new work to a reviewer/triage/goal-pilot agent.
        if not config.triage_follow_up_agent:
            return
        if config.triage_follow_up_agent not in config.agents:
            errors.append(
                f"review.triage_follow_up_agent '{config.triage_follow_up_agent}' "
                f"not found in agents. Available: {list(config.agents.keys())}"
            )

    def _validate_health_review_requires_agent(
        self, config: "Config", errors: list[str]
    ) -> None:
        # Cross-field invariant (#6776): a positive health-review interval with
        # no triage agent is silently disabled at runtime
        # (health_review_interval_minutes() returns 0). Reject the pair so the
        # misconfiguration fails loudly rather than degrading; 0/absent is the
        # documented disable value and a positive interval needs an agent.
        interval = config.triage.health_review.interval_minutes
        if interval > 0 and not config.triage_review_agent:
            errors.append(
                f"triage.health_review.interval_minutes is {interval} but no "
                "triage agent is configured. Set review.triage_review_agent, or "
                "use 0 to disable the periodic health review."
            )

    def _validate_exchange_mode(
        self,
        exchange_mode: str,
        config: "Config",
        errors: list[str],
    ) -> None:
        allowed_modes = {"via-draft-pr", "via-mcp", "via-local-loop", "auto"}
        if exchange_mode not in allowed_modes:
            errors.append(
                f"review.exchange.mode '{exchange_mode}' is invalid. "
                f"Allowed: {sorted(allowed_modes)}"
            )

    def _validate_probe_schedule(self, config: "Config", errors: list[str]) -> None:
        schedule = config.review_exchange_probe_schedule
        allowed_schedules = {"startup", "daily", "interval", "manual"}
        if schedule not in allowed_schedules:
            errors.append(
                f"review.exchange.probe.schedule '{schedule}' is invalid. "
                f"Allowed: {sorted(allowed_schedules)}"
            )
        if schedule == "interval" and config.review_exchange_probe_interval_days < 1:
            errors.append(
                "review.exchange.probe.interval_days must be >= 1 when schedule=interval."
            )

        if config.review_exchange_max_rounds < 1:
            errors.append("review.exchange.loop.max_rounds must be >= 1.")
        if config.review_exchange_max_no_progress < 1:
            errors.append("review.exchange.loop.max_no_progress must be >= 1.")

    def _validate_supported_exchange_pair(
        self,
        exchange_mode: str,
        config: "Config",
        errors: list[str],
    ) -> None:
        if exchange_mode != "via-mcp" or not config.review_enabled:
            return
        from ..review_exchange_registry import SUPPORTED_MCP_PAIRS
        if not config.code_review_agent:
            errors.append(
                "review.exchange.mode is via-mcp but review.default is not set."
            )
            return

        pairs = self._collect_exchange_pairs(config)
        if not pairs:
            return

        unsupported_pairs = self._unsupported_exchange_pairs(
            pairs,
            config,
            SUPPORTED_MCP_PAIRS,
        )
        if unsupported_pairs:
            errors.append(
                "review.exchange.mode is via-mcp but unsupported ai_system pair(s) configured: "
                f"{unsupported_pairs}. Use via-local-loop or update the MCP allowlist."
            )

    @staticmethod
    def _collect_exchange_pairs(config: "Config") -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for label, agent in config.agents.items():
            if config.triage_review_agent and label == config.triage_review_agent:
                continue
            if agent.skip_review:
                continue
            reviewer_label = config.get_reviewer_for_agent(label)
            if not reviewer_label or reviewer_label not in config.agents:
                continue
            pairs.append((label, reviewer_label))
        return pairs

    @staticmethod
    def _unsupported_exchange_pairs(
        pairs: list[tuple[str, str]],
        config: "Config",
        supported_pairs,
    ) -> list[str]:
        unsupported_pairs = []
        for coder_label, reviewer_label in pairs:
            coder_system = config.agents[coder_label].ai_system
            reviewer_system = config.agents[reviewer_label].ai_system
            if (coder_system, reviewer_system) not in supported_pairs:
                unsupported_pairs.append(
                    f"{coder_label}->{reviewer_label} ({coder_system}->{reviewer_system})"
                )
        return unsupported_pairs
