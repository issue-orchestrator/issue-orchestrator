"""Field-value validation rules shared by config loading.

Home for per-field config value rules extracted from ``Config.validate()``.
Loaders in ``config_sections.py`` assign raw YAML values; these rules
report malformed values loudly instead of any silent coercion. Tracking
issue #6532 aims to derive rules for settings-visible fields from the
settings registry so each constraint has exactly one owner.
"""

from __future__ import annotations

from typing import Any

VALID_NIT_POLICIES = frozenset({"ignore", "surface", "address"})


def resolve_tech_lead_watch_label(
    tech_lead_review_label: str | None, code_reviewed_label: str | None
) -> str:
    """Single owner for the label that selects PRs for tech_lead batch review.

    Every consumer of the tech_lead watch set (threshold fact gathering,
    manifest building, setup-wizard prompt generation) must derive the label
    through this function; deriving it locally is how the threshold trigger
    and the audited PR set drift apart.
    """
    return tech_lead_review_label or code_reviewed_label or "code-reviewed"


def normalize_optional_mapping(value: Any) -> Any:
    """Normalize a YAML mapping value at load time.

    YAML's empty-value spelling (``by_agent:`` with nothing under it)
    parses as None and idiomatically means an empty mapping - treat it as
    one. Real mappings are copied; any other shape passes through
    unchanged so ``Config.validate()`` reports it loudly.
    """
    if value is None:
        return {}
    return dict(value) if isinstance(value, dict) else value


def validate_review_nit_policy(default_policy: object, by_agent: object) -> list[str]:
    """Validate review nit policy settings (``review.nits.*``)."""
    errors: list[str] = []
    if default_policy not in VALID_NIT_POLICIES:
        errors.append("review.nits.default_policy must be one of: ignore, surface, address")
    if not isinstance(by_agent, dict):
        errors.append("review.nits.by_agent must be a mapping of coder agent label to policy")
        return errors
    for agent_label, policy in by_agent.items():
        if not isinstance(agent_label, str):
            errors.append(
                f"review.nits.by_agent key {agent_label!r} must be a string agent label"
            )
        if policy not in VALID_NIT_POLICIES:
            errors.append(
                f"review.nits.by_agent.{agent_label} must be one of: ignore, surface, address"
            )
    return errors
