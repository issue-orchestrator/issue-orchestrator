"""The durable record of an engine's latest start (#7490).

Written once when a Repository Engine starts, into its state directory, so a
reader outside the engine (the improver's staging, an operator) can separate
what happened *since this start* from history, and can read the tech lead's
EFFECTIVE charter (the charter and per-action ceilings after config overrides)
as the engine resolved it, not as the source defaults or today's config say.

Nothing else records these: the start is a log line that rotates away and the
merged config is a trace event the timeline drops.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ENGINE_START_SCHEMA_VERSION = 2


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleDials(_Closed):
    """One charter role's dials, as the engine resolved them."""

    enabled: bool
    depth: str
    authority: str


class ActionAuthority(_Closed):
    """What the engine would decide for one tech-lead action kind."""

    role: str
    required_depth: str
    binding: str
    #: The per-action ceiling and the setting it came from.
    action_ceiling: str
    ceiling_source: str
    outcome: str
    reason_code: str


class EffectiveCharter(_Closed):
    """The tech lead's effective charter at engine start.

    ``actions`` holds every classified kind the engine would decide, keyed by
    kind; a finding promotion is absent when ``promotion_lane`` is ``off``.
    """

    roles: dict[str, RoleDials]
    actions: dict[str, ActionAuthority]
    promotion_lane: Literal["off", "gated", "auto"]


class EngineStartRecord(_Closed):
    schema_version: Literal[2] = ENGINE_START_SCHEMA_VERSION
    started_at: AwareDatetime
    #: ``owner/repo`` the engine works, as its config said at this start: the
    #: engine's identity for any outside reader (#7567). A config edited or
    #: re-selected later does not re-attribute this state. None when the
    #: config named no repository.
    repo: str | None
    #: The io source commit the engine runs (its code, not the target repo);
    #: None for an install with no source identity (a wheel).
    engine_commit: str | None = Field(min_length=1)
    package_version: str
    #: The target repository checkout the engine serves, and its HEAD then.
    repo_root: str
    repo_head: str | None
    charter: EffectiveCharter


__all__ = [
    "ENGINE_START_SCHEMA_VERSION",
    "ActionAuthority",
    "EffectiveCharter",
    "EngineStartRecord",
    "RoleDials",
]
