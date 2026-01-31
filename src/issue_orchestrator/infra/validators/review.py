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
    """

    def validate(self, config: "Config") -> list[str]:
        errors: list[str] = []

        self._validate_review_defaults(config, errors)
        self._validate_triage_agent(config, errors)

        exchange_mode = config.review_exchange_mode
        coder_label = config.review_exchange_coder
        reviewer_label = config.review_exchange_reviewer

        self._validate_exchange_mode(exchange_mode, coder_label, reviewer_label, config, errors)
        self._validate_probe_schedule(config, errors)
        self._validate_supported_exchange_pair(exchange_mode, coder_label, reviewer_label, config, errors)

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

    def _validate_exchange_mode(
        self,
        exchange_mode: str,
        coder_label: str | None,
        reviewer_label: str | None,
        config: "Config",
        errors: list[str],
    ) -> None:
        allowed_modes = {"via-draft-pr", "via-mcp", "auto"}
        if exchange_mode not in allowed_modes:
            errors.append(
                f"review.exchange.mode '{exchange_mode}' is invalid. "
                f"Allowed: {sorted(allowed_modes)}"
            )
        if exchange_mode in {"via-mcp", "auto"} and (not coder_label or not reviewer_label):
            errors.append(
                "review.exchange.mode requires review.exchange.agent_pair "
                "with both coder and reviewer when using via-mcp or auto."
            )
        for label, role in ((coder_label, "coder"), (reviewer_label, "reviewer")):
            if label and label not in config.agents:
                errors.append(
                    f"review.exchange.agent_pair.{role} '{label}' not found in agents. "
                    f"Available: {list(config.agents.keys())}"
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

    def _validate_supported_exchange_pair(
        self,
        exchange_mode: str,
        coder_label: str | None,
        reviewer_label: str | None,
        config: "Config",
        errors: list[str],
    ) -> None:
        if exchange_mode != "via-mcp":
            return
        if not coder_label or not reviewer_label:
            return
        from ..ai_systems_config import get_ai_systems_config
        from ..review_exchange_registry import supports_mcp_pair
        from ...ports.session_log import detect_ai_system_from_command

        def _resolve_system(label: str) -> str:
            agent = config.agents[label]
            if agent.ai_system:
                return agent.ai_system
            detected = detect_ai_system_from_command(agent.command)
            if detected:
                return detected
            systems = get_ai_systems_config(config.repo_root)
            return systems.default_ai_system

        coder_system = _resolve_system(coder_label)
        reviewer_system = _resolve_system(reviewer_label)
        if not supports_mcp_pair(coder_system, reviewer_system):
            errors.append(
                "review.exchange.mode is via-mcp but agent pair is not supported: "
                f"{coder_label}({coder_system}) -> {reviewer_label}({reviewer_system}). "
                "Switch to via-draft-pr or update the MCP allowlist."
            )
