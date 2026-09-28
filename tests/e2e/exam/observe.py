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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.review_scope import extract_issue_number_from_pr
from issue_orchestrator.control.review_validity import evaluate_review_validity
from issue_orchestrator.domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_run_artifacts import TECH_LEAD_DATA_DIRNAME
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher
from issue_orchestrator.testing.exam.upgrade import UpgradeFacts
from issue_orchestrator.testing.exam import (
    ExamObservation,
    GitHubCallCounts,
    PullRequestFact,
    PullRequestState,
    RunEnd,
    TechLeadActionFact,
    TechLeadRunFact,
    WorkItemFact,
)
from issue_orchestrator.observation.no_progress import find_repeating_failures
from issue_orchestrator.testing.exam.screens import SCREEN_QUOTE_CHARS, render_recording, silent_screen
from issue_orchestrator.testing.exam.stall import ItemEvent, concerns_item, stall_facts
from issue_orchestrator.testing.exam.tech_lead import (
    ProposedAction,
    executed_receipts,
    resolve_dispositions,
)

from tests.e2e.fixtures import _github_adapter
from tests.e2e.exam.seeding import describe_rollup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackedItem:
    role: str
    issue_number: int
    external_id: str
    """The title's ``[M0-760]`` id — the engine keys some events by it."""


def item_events(
    watcher: OrchestratorWatcher, item: TrackedItem, pr_numbers: Iterable[int]
) -> list[ItemEvent]:
    return [
        ItemEvent.from_stream(event)
        for event in watcher.view.global_events
        if concerns_item(
            event,
            issue_keys=frozenset({str(item.issue_number), item.external_id}),
            issue_number=item.issue_number,
            pr_numbers=frozenset(pr_numbers),
        )
    ]


def linked_pull_requests(repo: str, issue_number: int, *, state: str) -> list[PRInfo]:
    """The item's PRs, linked the way the engine links them, from COMPLETE reads.

    Not the search API ``get_prs_for_issue`` uses: the harness shares the
    engine's token, and a probe a minute would spend the 30/minute search
    budget the engine under test needs (#7298 was that budget running out).

    * ``open``: the complete open-PR walk.
    * ``all``: every PR numbered above the issue (PR numbers share the issue
      sequence, so every PR of the item is numbered above it).

    Both raise rather than return a partial list.
    """
    adapter = _github_adapter(repo)
    if state == "open":
        prs = adapter.list_open_prs_complete()
    elif state == "all":
        prs = adapter.list_prs_numbered_above(issue_number)
    else:
        raise ValueError(f"unsupported PR state {state!r}")
    return [pr for pr in prs if extract_issue_number_from_pr(pr, repo_slug=repo) == issue_number]


def _pr_fact(repo: str, pr: PRInfo, *, read_checks: bool) -> PullRequestFact:
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
        checks=describe_rollup(adapter.read_pr_status_check_rollup(fresh.number))
        if read_checks
        else "NOT_READ",
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
    parked_screen: str,
    extra_pr_numbers: Iterable[int] = (),
    read_checks: bool = True,
) -> WorkItemFact:
    """The item's final facts; ``read_checks=False`` skips the GraphQL rollup
    read for the cheap progress probe the drive loop makes."""
    adapter = _github_adapter(repo)
    issue = adapter.get_issue(item.issue_number)
    if issue is None:
        raise RuntimeError(f"issue #{item.issue_number} vanished while observing")
    linked = {pr.number: pr for pr in linked_pull_requests(repo, item.issue_number, state="all")}
    for number in extra_pr_numbers:
        if number not in linked:
            pr = adapter.get_pr(number)
            if pr is None:
                raise RuntimeError(f"seeded PR #{number} vanished while observing")
            linked[number] = pr
    facts = tuple(
        _pr_fact(repo, pr, read_checks=read_checks)
        for pr in sorted(linked.values(), key=lambda p: p.number)
    )
    open_prs = [adapter.get_pr(f.number) for f in facts if f.state.is_open]
    labels = LabelManager(config)
    events = item_events(watcher, item, (fact.number for fact in facts))
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
            parked_screen=parked_screen,
        ),
        events=tuple(event.name for event in events),
        approved_prs=frozenset(
            number
            for event in events
            if event.name == "review.approved"
            for number in (event.payload.get("pr_number"),)
            if isinstance(number, int) and not isinstance(number, bool)
        ),
    )


# ---------------------------------------------------------------------------
# Screens a session is parked on
# ---------------------------------------------------------------------------


def newest_recording(root: Path) -> Path | None:
    """The newest ``terminal-recording.jsonl`` under ``root``, if any."""
    found = [p for p in root.glob("**/terminal-recording.jsonl") if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def parked_screen(
    *, worktree_base: Path, active_sessions: Iterable[tuple[str, int]], issue_number: int
) -> str:
    """A LIVE session of the item sitting silent on a screen, or ``""``.

    Only sessions the engine still reports active are considered: a finished
    session's recording is silent too, and reporting it would invent a stall.
    Silence is the time since the recording was last written.
    """
    for session_name, number in active_sessions:
        if number != issue_number:
            continue
        recording = newest_recording(worktree_base / session_name)
        if recording is None:
            continue
        screen = render_recording(recording.read_text(encoding="utf-8").splitlines())
        silent = time.time() - recording.stat().st_mtime
        line = silent_screen(session_name, screen, silent_seconds=silent)
        if line:
            return line
    return ""


def _last_screen(recording: Path | None) -> str:
    if recording is None:
        return ""
    screen = render_recording(recording.read_text(encoding="utf-8").splitlines())
    return screen.text[-SCREEN_QUOTE_CHARS:]


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


def _anchor(row: Mapping[str, Any]) -> int:
    """The issue a run's events are published against (its anchor)."""
    for key in ("anchor_issue_number", "subject_issue_number"):
        value = row.get(key)
        if isinstance(value, int) and value > 0:
            return value
    raise RuntimeError(f"tech-lead run {row.get('run_id')!r} records no anchor or subject issue")


def observe_tech_lead_runs(
    state_dir: Path, watcher: OrchestratorWatcher, *, worktree_base: Path
) -> tuple[TechLeadRunFact, ...]:
    events = list(watcher.view.global_events)
    rows = _run_rows(state_dir)
    anchors = [_anchor(row) for row in rows]
    runs: list[TechLeadRunFact] = []
    for row in rows:
        data_dir = Path(str(row.get("artifact_dir") or "")) / TECH_LEAD_DATA_DIRNAME
        decision_path = data_dir / TECH_LEAD_DECISION_FILENAME
        report_path = data_dir / TECH_LEAD_REPORT_FILENAME
        decision: Mapping[str, Any] = {}
        if row.get("artifact_dir") and decision_path.exists():
            loaded = json.loads(decision_path.read_text(encoding="utf-8"))
            decision = loaded.get("decision", loaded) if isinstance(loaded, dict) else {}
        report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        raw_actions = [a for a in decision.get("proposed_actions", []) if isinstance(a, dict)]
        dispositions = resolve_dispositions(
            events,
            anchor_issue_number=_anchor(row),
            anchor_shared=anchors.count(_anchor(row)) > 1,
            run_failed=row["phase"] == "failed",
            actions=[
                ProposedAction(str(a.get("id", "")), str(a.get("action_type", "")))
                for a in raw_actions
            ],
        )
        actions = tuple(
            TechLeadActionFact(
                action_type=str(action.get("action_type", "")),
                target_number=action.get("target_number"),
                body=str(action.get("body", "")),
                disposition=disposition,
            )
            for action, disposition in zip(raw_actions, dispositions, strict=True)
        )
        findings = "\n".join(
            f"{f.get('title', '')}: {f.get('details', '')} {' '.join(map(str, f.get('evidence', [])))}"
            for f in decision.get("findings", [])
            if isinstance(f, dict)
        )
        last_screen = ""
        if not decision:
            # No decision: what the session last showed is the explanation.
            # The archive keeps the recording of a finished run; a live one
            # is still in its worktree, under the engine's ``issue-<N>``
            # session directory for the run's anchor.
            archived = Path(str(row.get("artifact_dir") or "")) / "terminal-recording.jsonl"
            last_screen = _last_screen(
                archived
                if row.get("artifact_dir") and archived.is_file()
                else newest_recording(worktree_base / f"issue-{_anchor(row)}")
            )
        runs.append(
            TechLeadRunFact(
                run_id=str(row["run_id"]),
                anchor_issue_number=_anchor(row),
                flavor=str(row["flavor"]),
                phase=str(row["phase"]),
                detail=str(row.get("detail") or ""),
                summary=str(decision.get("summary", "")),
                findings_text=findings,
                report_text=report,
                actions=actions,
                last_screen=last_screen,
            )
        )
    return tuple(runs)


def owned_numbers(repo: str, run_label: str, extra_prs: Iterable[int]) -> frozenset[int]:
    """Every issue/PR the run owns, read from GitHub (complete reads only).

    The harness creates its issues WITH the run label and the engine files
    its own issues with it (``filtering.label``), so "carries the run label"
    is ownership — plus every PR of those issues, and the PRs the harness
    seeded itself.
    """
    issues = [issue.number for issue in _github_adapter(repo).list_issues(labels=[run_label], state="all")]
    owned = set(issues) | set(extra_prs)
    for number in issues:
        owned.update(pr.number for pr in linked_pull_requests(repo, number, state="all"))
    return frozenset(owned)


def _github_calls(report: Mapping[str, Any] | None, ended_by: RunEnd) -> GitHubCallCounts | None:
    """The run's calls; missing ONLY because the engine exited mid-run."""
    if report is not None:
        return GitHubCallCounts.between(None, report)
    if ended_by is not RunEnd.ENGINE_EXITED:
        raise RuntimeError(
            f"engine returned no gh_audit report although it did not exit (ended by {ended_by.value})"
        )
    return None


def build_observation(
    *,
    case_id: str,
    engine_commit: str,
    items: tuple[WorkItemFact, ...],
    tech_lead_runs: tuple[TechLeadRunFact, ...],
    events: list[Mapping[str, Any]],
    owned: frozenset[int],
    gh_audit_report: Mapping[str, Any] | None,
    elapsed_seconds: float,
    ended_by: RunEnd,
    notes: tuple[str, ...],
    upgrade: UpgradeFacts | None = None,
) -> ExamObservation:
    return ExamObservation(
        case_id=case_id,
        engine_commit=engine_commit,
        items=items,
        tech_lead_runs=tech_lead_runs,
        tech_lead_receipts=executed_receipts(events),
        repeating_failures=find_repeating_failures(events),
        owned_numbers=owned,
        # The engine is a fresh process per run, so its report IS the run's calls.
        github_calls=_github_calls(gh_audit_report, ended_by),
        elapsed_seconds=elapsed_seconds,
        ended_by=ended_by,
        notes=notes,
        upgrade=upgrade,
    )
