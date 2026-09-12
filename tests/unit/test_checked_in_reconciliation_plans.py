"""Freshness guardrails for the reviewed lifecycle plans checked into this repo.

A lifecycle plan is a decision set an operator reviewed by hand at one moment
and then committed. Everything downstream protects the APPLY — the registry
refuses a stale revision, preflight admits the whole plan before writing any of
it — but nothing protected the plan itself between review and merge. #7248 round
3 found exactly that: a retained outcome whose reason said its fix "remains in
open PR #7238 and must merge before retirement", published on a branch whose own
merge base already contained that merge. The apply would have faithfully
recorded a reviewed decision whose premise was gone, leaving a case file open
that the plan's own stated condition said to close.

These run in the unit suite, so the publish gate is where that goes red.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from issue_orchestrator.control.tech_lead_case_file_lifecycle_reconciliation import (
    CaseFileLifecycleReconciliationPlan,
)
from issue_orchestrator.domain.tech_lead_findings import (
    TERMINAL_CASE_FILE_DISPOSITIONS,
)
from issue_orchestrator.entrypoints.cli_tech_lead import load_reconciliation_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_DIR = REPO_ROOT / "repo-specific" / "reconciliation"
RUNBOOK = REPO_ROOT / "docs" / "development" / "CASE_FILE_RECONCILIATION.md"
PLAN_PATHS = sorted(PLAN_DIR.glob("tech-lead-case-lifecycle-*.yaml"))

# A squash merge lands as one commit whose subject ends in "(#<pr>)".
_PR_REFERENCE = re.compile(r"#(\d+)")


def _merged_pull_requests() -> frozenset[int]:
    """Every PR number this repository's history records as merged."""
    subjects = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%s", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    merged = set()
    for subject in subjects:
        match = re.search(r"\(#(\d+)\)$", subject.strip())
        if match:
            merged.add(int(match.group(1)))
    return frozenset(merged)


def test_the_repository_has_checked_in_lifecycle_plans() -> None:
    """Guard the guard: a glob that matches nothing must not pass silently."""
    assert PLAN_PATHS, f"no lifecycle plans found under {PLAN_DIR}"


@pytest.mark.parametrize("path", PLAN_PATHS, ids=lambda p: p.name)
def test_every_checked_in_plan_loads_through_the_command_surface(path: Path) -> None:
    """The command must be able to read what the repository ships.

    Plan loading constructs each outcome's domain transition, so this also
    proves every checked-in plan can build the transitions its apply needs —
    the invariant a timezone-naive ``recorded_at`` used to slip past.
    """
    plan = load_reconciliation_plan(path)

    assert isinstance(plan, CaseFileLifecycleReconciliationPlan)
    assert plan.repository and plan.outcomes


@pytest.mark.parametrize("path", PLAN_PATHS, ids=lambda p: p.name)
def test_no_retained_outcome_rests_on_an_already_merged_pull_request(
    path: Path,
) -> None:
    """A reviewed "not yet fixed" must not cite a fix this repo already merged.

    A retained outcome (``active`` or ``needs_human``) asserts the class is not
    resolved. When its own reason names a pull request that is merged here, the
    reason a human wrote is no longer true and the decision needs re-review
    before the plan is published.

    The rule reads the ``reason`` field only, and that is the escape hatch as
    well as the check: ``reason`` carries the CLAIM, ``evidence`` carries the
    links. An outcome that stays retained even though a related PR merged — the
    merge fixed a neighbouring path, say — says so in prose and puts the PR in
    ``evidence``, which is not scanned. Nothing has to be weakened to express
    that, so the guard can stay strict about the case that actually bit.
    """
    merged = _merged_pull_requests()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    stale = [
        (outcome["signature"], number, outcome["reason"])
        for outcome in document["outcomes"]
        if outcome["disposition"] not in TERMINAL_CASE_FILE_DISPOSITIONS
        for number in {int(n) for n in _PR_REFERENCE.findall(outcome["reason"])}
        if number in merged
    ]

    assert not stale, "\n".join(
        f"{path.name}: {signature!r} is retained but its reason cites merged"
        f" PR #{number}; re-review the outcome (retire it, or state why it"
        f" stays retained without naming the PR in the reason):\n    {reason}"
        for signature, number, reason in stale
    )


def test_the_runbook_reports_the_counts_the_plans_actually_carry() -> None:
    """The published summary is checked, not maintained by hand.

    The same failure mode as the finding above, one level up: a prose total
    that stops matching the files it describes the moment one outcome is
    re-reviewed. Deriving it in a test means the runbook cannot drift.
    """
    outcomes = [
        outcome
        for path in PLAN_PATHS
        for outcome in yaml.safe_load(path.read_text(encoding="utf-8"))["outcomes"]
    ]
    terminal = sum(
        outcome["disposition"] in TERMINAL_CASE_FILE_DISPOSITIONS
        for outcome in outcomes
    )
    stated = re.search(
        r"Across both plans: (\d+) reviewed outcomes, (\d+) terminal, (\d+) retained",
        RUNBOOK.read_text(encoding="utf-8"),
    )

    assert stated is not None, f"{RUNBOOK} no longer states the plan totals"
    assert [int(group) for group in stated.groups()] == [
        len(outcomes),
        terminal,
        len(outcomes) - terminal,
    ]
