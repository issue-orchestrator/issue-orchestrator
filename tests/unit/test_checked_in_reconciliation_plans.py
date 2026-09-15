"""Publication guards for the reviewed lifecycle plans checked into this repo.

A lifecycle plan is a decision set an operator reviewed by hand at one moment
and then committed. Everything downstream protects the APPLY — the registry
refuses a stale revision, preflight admits the whole plan before writing any of
it — but nothing protected the plan itself between review and merge. #7248 round
3 found exactly that: a retained outcome whose reason said its fix "remains in
open PR #7238 and must merge before retirement", published on a branch whose own
merge base already contained that merge.

Round 4 then found that the guard written for it was worse than no guard. It
scanned this repository's ambient git history for merged pull requests, and:

* CI checks out one commit (``actions/checkout`` has defaulted to
  ``fetch-depth: 1`` since v2), so in the environment that gates publication it
  saw an empty history and passed vacuously — a silent degradation this repo's
  fail-fast policy does not allow from an authoritative guard;
* it recognized only squash subjects ending ``(#<n>)`` and missed the 777
  ``Merge pull request #<n> from …`` subjects in this repository's own history;
* and, fatally, it could not say WHICH repository a bare ``#<n>`` belonged to.
  These plans cover two repositories, so a porchpin number would be answered
  from issue-orchestrator's history. That is a false positive on a correct
  plan, and a gate that fails correct work is how guards get weakened.

The lesson is that "is this pull request merged?" is a GitHub question, not a
local-history one, and a unit test must not pretend otherwise. So the enforced
guards below are the ones that can be answered deterministically and offline
from the repository's own content, and the operator's pre-publish re-check is
documented in the runbook instead of being faked here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
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

# The two forms these plans use to name a numbered GitHub item.
_NUMBERED_ITEM = re.compile(r"#\d+|/(?:pull|issues)/\d+")

# fc1ac91c2e8f201b9ee469ab1cdffb622a58b926, "Fetch Tech Lead PR diffs and
# coordinate pattern case files (#7238)". A fixed historical instant, written
# down rather than read from ambient git history for the reason F6 established:
# a depth-1 CI checkout cannot see it, and a guard that silently answers "no
# history" is worse than no guard.
TRANSPORT_FIX_MERGED_AT = datetime(2026, 9, 11, 16, 3, 9, tzinfo=timezone.utc)


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _outcomes(path: Path) -> list[dict]:
    return _document(path)["outcomes"]


def _is_retained(outcome: dict) -> bool:
    return outcome["disposition"] not in TERMINAL_CASE_FILE_DISPOSITIONS


def test_the_repository_has_checked_in_lifecycle_plans() -> None:
    """Guard the guard: a glob that matches nothing must not pass silently."""
    assert PLAN_PATHS, f"no lifecycle plans found under {PLAN_DIR}"


@pytest.mark.parametrize("path", PLAN_PATHS, ids=lambda p: p.name)
def test_every_checked_in_plan_loads_through_the_command_surface(path: Path) -> None:
    """The command must be able to read what the repository ships.

    Plan loading constructs each outcome's domain transition, so this also
    proves every checked-in plan can build the transitions its apply needs —
    the invariant a timezone-naive ``recorded_at`` used to slip past. It earns
    its keep on ordinary editing too: the round 3 plan correction first landed
    with an unquoted ``#``, which YAML reads as a comment, and this caught it.
    """
    plan = load_reconciliation_plan(path)

    assert isinstance(plan, CaseFileLifecycleReconciliationPlan)
    assert plan.repository and plan.outcomes


@pytest.mark.parametrize("path", PLAN_PATHS, ids=lambda p: p.name)
def test_no_retained_reason_asserts_the_state_of_a_numbered_item(path: Path) -> None:
    """A retained decision may not rest on a claim that decays.

    This is the F5 shape rather than the F5 instance. A retained outcome
    (``active`` or ``needs_human``) asserts a class is not resolved. When its
    reason also asserts the STATE of a numbered GitHub item — "remains in open
    PR #7238", "tracker #229 is still open" — it has written down a fact about
    the world that can be overtaken between review and merge, and nothing in
    the apply path re-checks it. That is precisely what happened: the premise
    was false at the branch's own merge base and the apply would still have
    recorded the reviewed decision faithfully.

    The rule is deterministic and offline because it reads only the plan: no
    git history, no network, no clone-depth dependency, and no guessing which
    repository a number belongs to. ``reason`` carries the CLAIM and must stay
    durable prose; ``evidence`` carries the LINKS and is deliberately not
    scanned. So an outcome legitimately retained alongside related merged work
    says why in prose and cites the work under ``evidence`` — as
    ``review-exchange-restart-rederives-reviewer-verdict`` does with merged PR
    #7141 today. Nothing has to be weakened to express that.
    """
    offenders = [
        (outcome["signature"], match.group(0), outcome["reason"])
        for outcome in _outcomes(path)
        if _is_retained(outcome)
        for match in [_NUMBERED_ITEM.search(outcome["reason"])]
        if match is not None
    ]

    assert not offenders, "\n".join(
        f"{path.name}: {signature!r} is retained but its reason asserts the state"
        f" of {reference}. A reviewed plan cannot vouch for that between review"
        f" and merge. Re-review the outcome: retire it if the work landed, or"
        f" state the condition in durable prose and move the link to"
        f" 'evidence':\n    {reason}"
        for signature, reference, reason in offenders
    )


def test_the_merged_transport_fix_stays_retired() -> None:
    """Pin the exact correction round 3 made, so it cannot silently come back.

    ``fc1ac91c2e8f201b9ee469ab1cdffb622a58b926`` — "Fetch Tech Lead PR diffs and
    coordinate pattern case files (#7238)" — is an ancestor of ``origin/main``
    and carries the ``RepositoryHost`` diff-fetch fix this signature names, so
    the outcome's own stated retirement condition is met. The plan said
    ``active``. This asserts the corrected decision directly rather than
    re-deriving it from ambient history the test cannot trust.
    """
    signature = "tech-lead-batch-manifest-diff-fetch-blocked-by-gh-guard"
    plan = PLAN_DIR / "tech-lead-case-lifecycle-porchpin-2026-09.yaml"
    matching = [item for item in _outcomes(plan) if item["signature"] == signature]

    assert len(matching) == 1, f"{signature!r} is not in {plan.name}"
    outcome = matching[0]
    assert outcome["disposition"] in TERMINAL_CASE_FILE_DISPOSITIONS, (
        f"{signature!r} must stay retired: its transport fix merged in PR #7238"
        f" (fc1ac91), which is an ancestor of origin/main"
    )
    assert (
        "https://github.com/issue-orchestrator/issue-orchestrator/pull/7238"
        in outcome["evidence"]
    )


def test_the_plan_recording_the_merged_fix_is_dated_after_it_merged() -> None:
    """A reviewed transition may not be dated before the fact it records.

    ``recorded_at`` is not plan prose. It rides into
    :class:`CaseFileLifecycleTransition`, which is one reviewed, durable change
    to a case file, and lands in the registry as part of that transition's
    audit record. The round 3 correction moved this outcome to ``shipped`` on
    the strength of a merge from 2026-09-11T16:03:09Z while leaving the
    plan-wide timestamp at 2026-09-10T23:15:00Z, so applying it would have
    written durable authority claiming the fix shipped seventeen hours before
    the commit it cites existed (#7248 round 5 review F7).

    Correcting the timestamp before publication is safe rather than a second
    decision: Porchpin's shared registry has never been seeded, so no
    transition is recorded to contradict — and even where one were,
    ``CaseFileLifecycleTransition.same_intent`` deliberately excludes
    ``recorded_at``, so a replay still resolves to the first reservation.
    """
    plan = PLAN_DIR / "tech-lead-case-lifecycle-porchpin-2026-09.yaml"
    recorded_at = datetime.fromisoformat(_document(plan)["recorded_at"])

    assert recorded_at >= TRANSPORT_FIX_MERGED_AT, (
        f"{plan.name} records its outcomes at {recorded_at.isoformat()}, before"
        f" PR #7238 merged at {TRANSPORT_FIX_MERGED_AT.isoformat()}. It retires"
        f" a case file on the strength of that merge, so the transition it"
        f" writes would be dated before the fact it records; re-review the"
        f" decision set and advance recorded_at to the re-review time."
    )


def test_the_runbook_reports_the_counts_the_plans_actually_carry() -> None:
    """The published summary is checked, not maintained by hand.

    The same failure mode as the finding above, one level up: a prose total
    that stops matching the files it describes the moment one outcome is
    re-reviewed. Deriving it in a test means the runbook cannot drift.
    """
    outcomes = [outcome for path in PLAN_PATHS for outcome in _outcomes(path)]
    retained = sum(_is_retained(outcome) for outcome in outcomes)
    stated = re.search(
        r"Across both plans: (\d+) reviewed outcomes, (\d+) terminal, (\d+) retained",
        RUNBOOK.read_text(encoding="utf-8"),
    )

    assert stated is not None, f"{RUNBOOK} no longer states the plan totals"
    assert [int(group) for group in stated.groups()] == [
        len(outcomes),
        len(outcomes) - retained,
        retained,
    ]
