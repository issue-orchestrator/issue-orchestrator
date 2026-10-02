"""Shared budgeted-validation test doubles."""

from __future__ import annotations

from datetime import datetime, timedelta

from issue_orchestrator.domain.engine_activity import EngineActivityObservation


class NoEngineObservation:
    """The engine-activity port of a cycle that hosts only code-change suites:
    observing engines there is a bug."""

    def observe(self, *, now: datetime, recent: timedelta) -> EngineActivityObservation:
        raise AssertionError("a code-change suite never observes engine activity")


CODE_CHANGE_ONLY = NoEngineObservation()
