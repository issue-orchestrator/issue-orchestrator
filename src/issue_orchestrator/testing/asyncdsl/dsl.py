from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional, Any

from issue_orchestrator.domain.issue_key import StableIssueId
from issue_orchestrator.events.catalog import EventName

from .materializer import MaterializedView
from .errors import WaitTimeout, NoProgressTimeout
from .config import WatcherConfig

Predicate = Callable[[], bool]

def _event_name(event_type: str | EventName) -> str:
    if isinstance(event_type, EventName):
        return event_type.value
    return event_type

def _now() -> float:
    return time.monotonic()

def _deadline(timeout_s: float) -> float:
    return _now() + timeout_s

@dataclass
class IssueWatch:
    issue_key: StableIssueId
    view: MaterializedView
    cfg: WatcherConfig
    _notify: asyncio.Event

    def _issue(self):
        return self.view.issues.get(self.issue_key)

    def labels(self) -> set[str]:
        iv = self._issue()
        return set(iv.labels) if iv else set()

    def state(self) -> Optional[str]:
        iv = self._issue()
        return iv.state if iv else None

    def pr_number(self) -> Optional[int]:
        iv = self._issue()
        return iv.pr.number if iv else None

    def pr_is_draft(self) -> Optional[bool]:
        iv = self._issue()
        return iv.pr.draft if iv else None

    async def _await(self, predicate: Predicate, *, timeout_s: float, name: str) -> None:
        end = _deadline(timeout_s)
        while True:
            if predicate():
                return

            if (_now() - self.view.last_progress_monotonic) > self.cfg.no_progress_s:
                raise NoProgressTimeout(
                    message=f"No progress for {self.cfg.no_progress_s}s while waiting for {name} on {self.issue_key}",
                    diagnostics=self.diagnostics(),
                )

            remaining = end - _now()
            if remaining <= 0:
                raise WaitTimeout(
                    message=f"Timeout waiting for {name} on {self.issue_key} (timeout={timeout_s}s)",
                    diagnostics=self.diagnostics(),
                )

            try:
                await asyncio.wait_for(self._notify.wait(), timeout=min(self.cfg.poll_interval_s, remaining))
            except asyncio.TimeoutError:
                pass
            finally:
                self._notify.clear()

    def diagnostics(self) -> dict[str, Any]:
        iv = self._issue()
        return {
            "issue_key": self.issue_key,
            "last_event_id": self.view.last_event_id,
            "orchestrator_idle": self.view.orchestrator.idle,
            "issue": None if iv is None else {
                "labels": sorted(list(iv.labels)),
                "state": iv.state,
                "pr": {"number": iv.pr.number, "draft": iv.pr.draft, "labels": sorted(list(iv.pr.labels))},
                "apply_attempts": iv.apply_attempts,
                "reconcile_required": iv.reconcile_required,
                "updated_at": iv.updated_at,
            },
            "recent_issue_events": list(self.view.issue_events.get(self.issue_key, []))[-30:],
            "recent_global_events": list(self.view.global_events)[-30:],
        }

    async def has_label(self, label: str, *, timeout_s: Optional[float] = None) -> "IssueWatch":
        await self._await(lambda: label in self.labels(), timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"has_label({label})")
        return self

    async def lacks_label(self, label: str, *, timeout_s: Optional[float] = None) -> "IssueWatch":
        await self._await(lambda: label not in self.labels(), timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"lacks_label({label})")
        return self

    async def state_is(self, state: str, *, timeout_s: Optional[float] = None) -> "IssueWatch":
        await self._await(lambda: self.state() == state, timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"state_is({state})")
        return self

    async def terminal(self, state: str, *, timeout_s: Optional[float] = None) -> "IssueWatch":
        await self._await(lambda: self.state() == state, timeout_s=timeout_s or self.cfg.timeout_terminal_s, name=f"terminal({state})")
        return self

    async def has_pr(self, *, draft: Optional[bool] = None, timeout_s: Optional[float] = None) -> "IssueWatch":
        def pred():
            n = self.pr_number()
            if n is None:
                return False
            if draft is None:
                return True
            return self.pr_is_draft() == draft
        await self._await(pred, timeout_s=timeout_s or self.cfg.timeout_pr_s, name=f"has_pr(draft={draft})")
        return self

    async def pr_has_label(self, label: str, *, timeout_s: Optional[float] = None) -> "IssueWatch":
        def pred():
            iv = self._issue()
            return iv is not None and label in iv.pr.labels
        await self._await(pred, timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"pr_has_label({label})")
        return self

    async def event(
        self,
        event_type: str | EventName,
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout_s: Optional[float] = None,
    ) -> "IssueWatch":
        etype = _event_name(event_type)

        def pred() -> bool:
            events = self.view.issue_events.get(self.issue_key, [])
            for event in reversed(events):
                if event.get("type") != etype:
                    continue
                if predicate is None or predicate(event):
                    return True
            return False

        await self._await(pred, timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"event({etype})")
        return self

    async def not_thrashing(self) -> "IssueWatch":
        def pred():
            iv = self._issue()
            if iv is None:
                return True
            return (iv.apply_attempts <= self.cfg.max_issue_apply_attempts and
                    iv.reconcile_required <= self.cfg.max_issue_reconcile_required)
        await self._await(pred, timeout_s=self.cfg.timeout_fast_s, name="not_thrashing")
        return self

@dataclass
class SystemWatch:
    view: MaterializedView
    cfg: WatcherConfig
    _notify: asyncio.Event

    async def _await(self, predicate: Callable[[], bool], *, timeout_s: float, name: str) -> None:
        end = _deadline(timeout_s)
        while True:
            if predicate():
                return

            if (_now() - self.view.last_progress_monotonic) > self.cfg.no_progress_s:
                raise NoProgressTimeout(
                    message=f"No progress for {self.cfg.no_progress_s}s while waiting for {name}",
                    diagnostics=self.diagnostics(),
                )

            remaining = end - _now()
            if remaining <= 0:
                raise WaitTimeout(
                    message=f"Timeout waiting for {name} (timeout={timeout_s}s)",
                    diagnostics=self.diagnostics(),
                )

            try:
                await asyncio.wait_for(self._notify.wait(), timeout=min(self.cfg.poll_interval_s, remaining))
            except asyncio.TimeoutError:
                pass
            finally:
                self._notify.clear()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "last_event_id": self.view.last_event_id,
            "orchestrator": {
                "idle": self.view.orchestrator.idle,
                "paused": self.view.orchestrator.paused,
                "last_tick_id": self.view.orchestrator.last_tick_id,
            },
            "issues_seen": sorted(list(self.view.issues.keys()))[:50],
            "recent_global_events": list(self.view.global_events)[-50:],
        }

    async def idle(self, *, timeout_s: Optional[float] = None) -> "SystemWatch":
        await self._await(lambda: self.view.orchestrator.idle, timeout_s=timeout_s or self.cfg.timeout_idle_s, name="idle()")
        return self

    async def event(
        self,
        event_type: str | EventName,
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout_s: Optional[float] = None,
    ) -> "SystemWatch":
        etype = _event_name(event_type)

        def pred() -> bool:
            for event in reversed(self.view.global_events):
                if event.get("type") != etype:
                    continue
                if predicate is None or predicate(event):
                    return True
            return False

        await self._await(pred, timeout_s=timeout_s or self.cfg.timeout_fast_s, name=f"event({etype})")
        return self
