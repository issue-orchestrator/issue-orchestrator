"""The ``tech_lead.charter`` config block (#7330).

```yaml
tech_lead:
  charter:
    flow:        { depth: fix,         authority: execute }
    review_loop: { depth: fix,         authority: propose }
    abstraction: { depth: restructure, authority: propose }
    general:     { depth: workaround,  authority: propose }
    intake:      { enabled: false }
```

Validation is fail-fast by SHAPE: an unknown role, an unknown key inside a
role, a depth or authority outside its vocabulary, or a non-boolean
``enabled`` is a configuration error, never a silent default.

A role omitted from the block keeps its DEFAULT dials rather than switching
off: the settings form writes one dial at a time, and treating an absent role
as disabled would let a single edit silently disable every role the form did
not touch. Disabling a role is explicit: ``enabled: false``.

The defaults reproduce the pre-charter behaviour exactly: every named role is
``restructure``/``execute``, so the per-action ``tech_lead.authority.*`` modes
and ``tech_lead.findings.promote`` remain the only dials that decide until an
operator narrows a role.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.tech_lead_charter import (
    CHARTER_ACTION_CLASSES,
    DEFAULT_ROLE_CHARTERS,
    CharterBinding,
    CharterAuthority,
    CharterDepth,
    CharterRole,
    RoleCharter,
    TechLeadCharter,
)

TECH_LEAD_CHARTER_ROLES: tuple[str, ...] = tuple(role.value for role in CharterRole)
TECH_LEAD_CHARTER_DEPTHS: tuple[str, ...] = tuple(depth.value for depth in CharterDepth)
TECH_LEAD_CHARTER_AUTHORITIES: tuple[str, ...] = tuple(
    authority.value for authority in CharterAuthority
)
_ROLE_KEYS = ("enabled", "depth", "authority")

#: Action kinds that never run unattended (#7330), from the one classification.
DESTRUCTIVE_TECH_LEAD_ACTIONS: tuple[str, ...] = tuple(
    kind
    for kind, action_class in CHARTER_ACTION_CLASSES.items()
    if action_class.binding is CharterBinding.DESTRUCTIVE
)


def destructive_execute_error(action_type: str, mode: str) -> str | None:
    """The error for ``tech_lead.authority.<destructive>: execute``, else None.

    Accepting the value would make it a silent no-op: the charter refuses to run
    a destructive action unattended whatever the mode says.
    """
    if action_type not in DESTRUCTIVE_TECH_LEAD_ACTIONS or mode != "execute":
        return None
    return (
        f"tech_lead.authority.{action_type}: this action is destructive (reset"
        " from scratch) and always needs operator approval, so 'execute' is not"
        " allowed (#7330); remove the key or set it to propose"
    )


@dataclass
class RoleCharterConfig:
    """One role's dials as configured (plain values, for the settings form)."""

    depth: str
    authority: str
    enabled: bool = True

    @classmethod
    def default_for(cls, role: CharterRole) -> "RoleCharterConfig":
        default = DEFAULT_ROLE_CHARTERS[role]
        return cls(
            depth=default.depth.value,
            authority=default.authority.value,
            enabled=default.enabled,
        )

    @classmethod
    def from_value(cls, role: CharterRole, value: Any) -> "RoleCharterConfig":
        path = f"tech_lead.charter.{role.value}"
        default = cls.default_for(role)
        if value is None:
            return default
        if not isinstance(value, dict):
            raise ValueError(
                f"{path} must be a mapping with depth/authority/enabled, got"
                f" {type(value).__name__} ({value!r})"
            )
        unknown = sorted(set(value) - set(_ROLE_KEYS))
        if unknown:
            raise ValueError(
                f"{path} has unknown key(s) {', '.join(map(str, unknown))};"
                f" supported keys are {', '.join(_ROLE_KEYS)}"
            )
        enabled = value.get("enabled", default.enabled)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"{path}.enabled must be a boolean, got {type(enabled).__name__}"
                f" ({enabled!r})"
            )
        config = cls(
            depth=value.get("depth", default.depth),
            authority=value.get("authority", default.authority),
            enabled=enabled,
        )
        errors = config.errors(role)
        if errors:
            raise ValueError("; ".join(errors))
        return config

    def errors(self, role: CharterRole) -> list[str]:
        path = f"tech_lead.charter.{role.value}"
        errors: list[str] = []
        if self.depth not in TECH_LEAD_CHARTER_DEPTHS:
            errors.append(
                f"{path}.depth must be one of {list(TECH_LEAD_CHARTER_DEPTHS)},"
                f" got {self.depth!r}"
            )
        if self.authority not in TECH_LEAD_CHARTER_AUTHORITIES:
            errors.append(
                f"{path}.authority must be one of"
                f" {list(TECH_LEAD_CHARTER_AUTHORITIES)}, got {self.authority!r}"
            )
        return errors

    def to_role_charter(self) -> RoleCharter:
        return RoleCharter(
            depth=CharterDepth(self.depth),
            authority=CharterAuthority(self.authority),
            enabled=self.enabled,
        )

    def to_event_dict(self) -> dict[str, object]:
        return {"enabled": self.enabled, "depth": self.depth, "authority": self.authority}


def _default(role: CharterRole) -> Any:
    return field(default_factory=lambda: RoleCharterConfig.default_for(role))


@dataclass
class TechLeadCharterConfig:
    """Per-role dials; one attribute per :class:`CharterRole` value."""

    flow: RoleCharterConfig = _default(CharterRole.FLOW)
    review_loop: RoleCharterConfig = _default(CharterRole.REVIEW_LOOP)
    abstraction: RoleCharterConfig = _default(CharterRole.ABSTRACTION)
    platform: RoleCharterConfig = _default(CharterRole.PLATFORM)
    intake: RoleCharterConfig = _default(CharterRole.INTAKE)
    learning: RoleCharterConfig = _default(CharterRole.LEARNING)
    general: RoleCharterConfig = _default(CharterRole.GENERAL)

    @classmethod
    def from_mapping(cls, data: Any) -> "TechLeadCharterConfig":
        """Parse ``tech_lead.charter``; omission or null means every default."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(
                f"tech_lead.charter must be a mapping of role -> dials, got"
                f" {type(data).__name__} ({data!r})"
            )
        unknown = sorted(str(key) for key in set(data) - set(TECH_LEAD_CHARTER_ROLES))
        if unknown:
            raise ValueError(
                f"tech_lead.charter has unknown role(s) {', '.join(unknown)};"
                f" roles are {', '.join(TECH_LEAD_CHARTER_ROLES)}"
            )
        return cls(
            **{
                role.value: RoleCharterConfig.from_value(role, data.get(role.value))
                for role in CharterRole
            }
        )

    def role(self, role: CharterRole) -> RoleCharterConfig:
        value = getattr(self, role.value)
        assert isinstance(value, RoleCharterConfig)
        return value

    def startup_errors(self) -> list[str]:
        """Re-validate values that may have been set without YAML (settings form)."""
        return [error for role in CharterRole for error in self.role(role).errors(role)]

    def to_charter(self) -> TechLeadCharter:
        """The domain charter the policy owner decides against."""
        return TechLeadCharter(
            roles={role: self.role(role).to_role_charter() for role in CharterRole}
        )

    def to_event_dict(self) -> dict[str, dict[str, object]]:
        return {role.value: self.role(role).to_event_dict() for role in CharterRole}

