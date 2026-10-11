"""Compose the inventory of Control Center's engines for entrypoints (#7567)."""

from __future__ import annotations

from ..adapters.registered_engine_inventory import RegisteredEngineInventory, engine_at
from ..adapters.repository_engine_supervisor import DefaultSupervisorOps
from ..ports.engine_activity import EngineInventory


def control_center_engine_inventory() -> EngineInventory:
    """Every engine the Control Center registry holds that runs, or ran lately."""
    return RegisteredEngineInventory(supervisor=DefaultSupervisorOps())


__all__ = ["control_center_engine_inventory", "engine_at"]
