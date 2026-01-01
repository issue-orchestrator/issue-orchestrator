from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class WatcherConfig:
    poll_interval_s: float = 0.25

    timeout_fast_s: float = 20.0
    timeout_pr_s: float = 60.0
    timeout_terminal_s: float = 240.0
    timeout_idle_s: float = 90.0

    no_progress_s: float = 120.0

    resync_on_gap: bool = True

    diag_max_events: int = 200

    max_issue_apply_attempts: int = 8
    max_issue_reconcile_required: int = 6
