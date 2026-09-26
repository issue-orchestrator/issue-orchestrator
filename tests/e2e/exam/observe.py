"""Turn a finished exam run into an :class:`ExamObservation`.

Sources, all present at every commit the exam targets:

* GitHub (issue labels/state, every PR linked to the item, branches, checks);
* the engine's event stream as the watcher collected it (per-item transitions,
  review-exchange role timeouts, tech-lead action receipts);
* the engine's state directory (``tech_lead_runs.sqlite`` and the archived
  ``tech-lead-data/`` of each run);
* the engine's own gh_audit report (calls per endpoint class).

The refusing gate is asked of the engine's gate OWNER
(``control.review_validity``) on the final GitHub state rather than
re-derived, so the exam cannot drift from the policy it reports on.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.review_validity import evaluate_review_validity
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_run_artifacts import TECH_LEAD_DATA_DIRNAME
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher
from issue_orchestrator.testing.exam import (
    ExamObservation,
    GitHubCallCounts,
    PullRequestFact,
    PullRequestState,
    RunEnd,
    TechLeadActionDisposition,
    TechLeadActionFact,
    TechLeadRunFact,
    WorkItemFact,
)
from issue_orchestrator.testing.exam.stall import ItemEvent, stall_facts

from tests.e2e.fixtures import _github_adapter
from tests.e2e.exam.seeding import describe_rollup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackedItem:
    role: str
    issue_number: int


def item_events(watcher: OrchestratorWatcher, repo: str, issue_number: int) -> list[ItemEvent]:
    key = GitHubIssueKey(repo=repo, external_id=str(issue_number)).stable_id()
    raw = watcher.view.issue_events.get(key, ())
    return [ItemEvent.from_stream(event) for event in raw]


def _pr_fact(repo: str, pr: PRInfo) -> PullRequestFact:
    adapter = _github_adapter(repo)
    fresh = adapter.get_pr(pr.number)
    if fresh is None:
        raise RuntimeError(f"PR #{pr.number} vanished while observing")
    state = PullRequestState.from_github(
        state=fresh.state, draft=fresh.draft, merged=fresh.state == "merged"
    )
    return PullRequestFact(
        number=fresh.number,
        state=state,
        labels=frozenset(fresh.labels),
        branch=fresh.branch,
        branch_exists=adapter.branch_exists(fresh.branch),
        checks=describe_rollup(adapter.read_pr_status_check_rollup(fresh.number)),
    )


def _refusing_gate(config: Config, labels: LabelManager, issue: Any, open_pr: PRInfo | None) -> str:
    if open_pr is None:
        return ""
    validity = evaluate_review_validity(
        config=config, label_manager=labels, issue=issue, pr=open_pr
    )
    if validity.valid:
        return ""
    blocking = f" ({', '.join(validity.blocking_labels)})" if validity.blocking_labels else ""
    return f"review_validity:{validity.reason}{blocking}"


def observe_item(
    *,
    repo: str,
    config: Config,
    item: TrackedItem,
    watcher: OrchestratorWatcher,
    extra_pr_numbers: Iterable[int] = (),
) -> WorkItemFact:
    adapter = _github_adapter(repo)
    issue = adapter.get_issue(item.issue_number)
    if issue is None:
        raise RuntimeError(f"issue #{item.issue_number} vanished while observing")
    linked = {pr.number: pr for pr in adapter.get_prs_for_issue(item.issue_number, state="all")}
    for number in extra_pr_numbers:
        if number not in linked:
            pr = adapter.get_pr(number)
            if pr is None:
                raise RuntimeError(f"seeded PR #{number} vanished while observing")
            linked[number] = pr
    facts = tuple(_pr_fact(repo, pr) for pr in sorted(linked.values(), key=lambda p: p.number))
    open_prs = [adapter.get_pr(f.number) for f in facts if f.state.is_open]
    labels = LabelManager(config)
    events = item_events(watcher, repo, item.issue_number)
    return WorkItemFact(
        role=item.role,
        issue_number=item.issue_number,
        issue_state=str(issue.state),
        issue_labels=frozenset(issue.labels),
        pull_requests=facts,
        stall=stall_facts(
            events,
            refusing_gate=_refusing_gate(config, labels, issue, open_prs[-1] if open_prs else None),
            blocking_labels=labels.get_blocking(list(issue.labels)),
        ),
        events=tuple(event.name for event in events),
    )


# ---------------------------------------------------------------------------
# Tech-lead runs
# ---------------------------------------------------------------------------


def _run_rows(state_dir: Path) -> list[Mapping[str, Any]]:
    db = state_dir / "tech_lead_runs.sqlite"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM tech_lead_run_records ORDER BY started_at"
        )]
    finally:
        conn.close()


def terminal_tech_lead_runs(state_dir: Path) -> list[Mapping[str, Any]]:
    return [row for row in _run_rows(state_dir) if row["phase"] != "running"]


def _dispositions(events: Iterable[Mapping[str, Any]]) -> dict[str, TechLeadActionDisposition]:
    """Action type -> what the engine reported doing with it.

    Receipts carry the action TYPE and the anchor, not the proposal id, so
    they are matched by type; the GitHub outcome, not this, is the ground
    truth for destruction.
    """
    by_type: dict[str, TechLeadActionDisposition] = {}
    for event in events:
        payload = event.get("payload") or {}
        action = payload.get("action") or payload.get("action_type")
        if not isinstance(action, str):
            continue
        if event.get("type") == "tech_lead.action_executed":
            by_type[action] = TechLeadActionDisposition.EXECUTED
        elif event.get("type") == "tech_lead.action_proposed":
            by_type.setdefault(action, TechLeadActionDisposition.PROPOSED)
    return by_type


def observe_tech_lead_runs(
    state_dir: Path, watcher: OrchestratorWatcher
) -> tuple[TechLeadRunFact, ...]:
    events = list(watcher.view.global_events)
    rejected = any(e.get("type") == "tech_lead.decision_rejected" for e in events)
    dispositions = _dispositions(events)
    runs: list[TechLeadRunFact] = []
    for row in _run_rows(state_dir):
        data_dir = Path(str(row.get("artifact_dir") or "")) / TECH_LEAD_DATA_DIRNAME
        decision_path = data_dir / TECH_LEAD_DECISION_FILENAME
        report_path = data_dir / TECH_LEAD_REPORT_FILENAME
        decision: Mapping[str, Any] = {}
        if row.get("artifact_dir") and decision_path.exists():
            loaded = json.loads(decision_path.read_text(encoding="utf-8"))
            decision = loaded.get("decision", loaded) if isinstance(loaded, dict) else {}
        report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        run_rejected = rejected or row["phase"] == "failed"
        actions = tuple(
            TechLeadActionFact(
                action_type=str(action.get("action_type", "")),
                target_number=action.get("target_number"),
                body=str(action.get("body", "")),
                disposition=TechLeadActionDisposition.REJECTED
                if run_rejected
                else dispositions.get(str(action.get("action_type", "")), TechLeadActionDisposition.UNKNOWN),
            )
            for action in decision.get("proposed_actions", [])
            if isinstance(action, dict)
        )
        findings = "\n".join(
            f"{f.get('title', '')}: {f.get('details', '')} {' '.join(map(str, f.get('evidence', [])))}"
            for f in decision.get("findings", [])
            if isinstance(f, dict)
        )
        runs.append(
            TechLeadRunFact(
                run_id=str(row["run_id"]),
                flavor=str(row["flavor"]),
                phase=str(row["phase"]),
                detail=str(row.get("detail") or ""),
                summary=str(decision.get("summary", "")),
                findings_text=findings,
                report_text=report,
                actions=actions,
            )
        )
    return tuple(runs)


def build_observation(
    *,
    case_id: str,
    engine_commit: str,
    items: tuple[WorkItemFact, ...],
    tech_lead_runs: tuple[TechLeadRunFact, ...],
    gh_audit_report: Mapping[str, Any],
    elapsed_seconds: float,
    ended_by: RunEnd,
    notes: tuple[str, ...],
) -> ExamObservation:
    return ExamObservation(
        case_id=case_id,
        engine_commit=engine_commit,
        items=items,
        tech_lead_runs=tech_lead_runs,
        # The engine is a fresh process per run, so its report IS the run's calls.
        github_calls=GitHubCallCounts.between(None, gh_audit_report),
        elapsed_seconds=elapsed_seconds,
        ended_by=ended_by,
        notes=notes,
    )
