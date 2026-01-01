from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Deque
from collections import deque

from .models import IssueView, OrchestratorView, PRView
from .errors import EventGapDetected

def _ensure_int(x: Any) -> int:
    if isinstance(x, bool):
        raise TypeError("event_id must be int, got bool")
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.isdigit():
        return int(x)
    raise TypeError(f"event_id must be int, got {type(x)}={x!r}")

@dataclass
class MaterializedView:
    last_event_id: int = 0
    orchestrator: OrchestratorView = field(default_factory=OrchestratorView)
    issues: Dict[str, IssueView] = field(default_factory=dict)

    global_events: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    issue_events: Dict[str, Deque[dict[str, Any]]] = field(default_factory=dict)

    last_progress_monotonic: float = field(default_factory=time.monotonic)

    def set_diag_limits(self, max_events: int) -> None:
        self.global_events = deque(self.global_events, maxlen=max_events)

    def _push_event(self, event: dict[str, Any]) -> None:
        self.global_events.append(event)
        ik = event.get("issue_key")
        if ik:
            dq = self.issue_events.get(ik)
            if dq is None:
                dq = deque(maxlen=self.global_events.maxlen)
                self.issue_events[ik] = dq
            dq.append(event)

    def _mark_progress(self) -> None:
        self.last_progress_monotonic = time.monotonic()

    def apply_snapshot(self, raw: dict[str, Any]) -> None:
        snap_id = _ensure_int(raw.get("snapshot_id"))
        orch_raw = raw.get("orchestrator", {}) or {}
        self.orchestrator = OrchestratorView(
            idle=bool(orch_raw.get("idle", False)),
            paused=bool(orch_raw.get("paused", False)),
            last_tick_id=orch_raw.get("last_tick_id"),
        )
        self.issues = {}
        for issue_key, iv in (raw.get("issues", {}) or {}).items():
            labels = set(iv.get("labels", []) or [])
            pr_raw = iv.get("pr", {}) or {}
            pr = PRView(
                number=pr_raw.get("number"),
                draft=pr_raw.get("draft"),
                labels=set(pr_raw.get("labels", []) or []),
            )
            self.issues[issue_key] = IssueView(
                issue_key=issue_key,
                labels=labels,
                state=iv.get("state"),
                pr=pr,
                updated_at=iv.get("updated_at"),
                apply_attempts=int(iv.get("apply_attempts", 0) or 0),
                reconcile_required=int(iv.get("reconcile_required", 0) or 0),
            )
        self.last_event_id = max(self.last_event_id, snap_id)
        self._push_event({"event_id": self.last_event_id, "type": "snapshot_applied"})
        self._mark_progress()

    def apply_event(self, event: dict[str, Any], *, gap_check: bool = True) -> None:
        eid = _ensure_int(event.get("event_id"))
        if gap_check and eid != self.last_event_id + 1 and eid > self.last_event_id:
            raise EventGapDetected(expected_next=self.last_event_id + 1, received=eid, last_seen=self.last_event_id)

        if eid <= self.last_event_id:
            return  # ignore stale/out-of-order

        self.last_event_id = eid
        self._push_event(event)
        # Any event receipt counts as progress; don't fail while the stream is active.
        self._mark_progress()

        etype = event.get("type")
        payload = event.get("payload", {}) or {}
        issue_key = event.get("issue_key")

        if etype in ("orchestrator_idle", "orchestrator_active"):
            self.orchestrator.idle = (etype == "orchestrator_idle")
            self._mark_progress()
            return

        if etype in ("tick_complete", "tick_completed"):
            self.orchestrator.last_tick_id = payload.get("tick_id", self.orchestrator.last_tick_id)
            if "idle" in payload:
                self.orchestrator.idle = bool(payload.get("idle"))
            # Ticks indicate the orchestrator is alive even if no issue events fire yet.
            self._mark_progress()
            return

        if etype in ("issue_view_changed", "issue_labels_changed", "issue_state_changed", "pr_view_changed"):
            if not issue_key:
                return
            iv = self.issues.get(issue_key) or IssueView(issue_key=issue_key)
            self.issues[issue_key] = iv

            if "labels" in payload:
                iv.labels = set(payload.get("labels") or [])
            if "added" in payload or "removed" in payload:
                added = set(payload.get("added") or [])
                removed = set(payload.get("removed") or [])
                iv.labels = (iv.labels | added) - removed
            if "state" in payload:
                iv.state = payload.get("state")
            if "updated_at" in payload:
                iv.updated_at = payload.get("updated_at")

            pr_raw = payload.get("pr")
            if pr_raw is not None:
                iv.pr.number = pr_raw.get("number", iv.pr.number)
                iv.pr.draft = pr_raw.get("draft", iv.pr.draft)
                if "labels" in pr_raw:
                    iv.pr.labels = set(pr_raw.get("labels") or [])
            else:
                if "pr_number" in payload:
                    iv.pr.number = payload.get("pr_number")
                if "draft" in payload:
                    iv.pr.draft = payload.get("draft")
                if "pr_labels" in payload:
                    iv.pr.labels = set(payload.get("pr_labels") or [])
                if "labels" in payload and etype == "pr_view_changed":
                    iv.pr.labels = set(payload.get("labels") or [])
                if "added" in payload or "removed" in payload:
                    added = set(payload.get("added") or [])
                    removed = set(payload.get("removed") or [])
                    iv.pr.labels = (iv.pr.labels | added) - removed

            self._mark_progress()
            return

        if etype == "apply_attempted" and issue_key:
            iv = self.issues.setdefault(issue_key, IssueView(issue_key=issue_key))
            iv.apply_attempts += 1
            self._mark_progress()
            return

        if etype == "reconciliation_required" and issue_key:
            iv = self.issues.setdefault(issue_key, IssueView(issue_key=issue_key))
            iv.reconcile_required += 1
            self._mark_progress()
            return

        if payload.get("progress", False):
            self._mark_progress()
