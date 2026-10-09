"""The rulings of the work a tech-lead run covers (#8347).

A batch review approved PRs against rulings it was never told (porchpin#379's
failure class): the launch prompt carried only the anchor issue's rulings. A
tech-lead run is bound by the rulings of every other issue whose work it
covers - a batch review's PRs' issues, a health review's problem cohort and
the blocked items it triages - read fresh, through the standing-rulings owner,
at launch and again at every validation retry. Which issues those are is
recorded once, at launch, in the run's launch authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.blocked_item_triage import TriageAgenda
    from ..domain.tech_lead_manifest import TechLeadManifest
    from ..ports.launch_prompt import LaunchPromptProvider
    from .tech_lead_run_inputs import LaunchAuthorityTransfer


def covered_issues(
    pr_issues: Mapping[int, tuple[int, ...]],
    problem_issue_numbers: tuple[int, ...],
    triaged: frozenset[int],
    *,
    anchor: int,
) -> dict[int, tuple[int, ...]]:
    """The other issues whose work a tech-lead run covers, with their PRs in it:
    a batch's PRs' issues (*pr_issues*), a health review's problem cohort and
    the blocked items it triages. The anchor's own rulings come with its launch
    prompt, so they are not repeated."""
    covered = dict(pr_issues)
    for number in (*problem_issue_numbers, *sorted(triaged)):
        covered.setdefault(number, ())
    covered.pop(anchor, None)
    return covered


def launch_covered_work(
    manifest: "TechLeadManifest | None",
    problem_issue_numbers: tuple[int, ...],
    triage_agenda: "TriageAgenda",
    *,
    anchor: int,
) -> dict[int, tuple[int, ...]]:
    """:func:`covered_issues` of a launch: its manifest, cohort and agenda."""
    return covered_issues(
        manifest.covered_issues() if manifest is not None else {},
        problem_issue_numbers,
        frozenset(item.issue_number for item in triage_agenda.items),
        anchor=anchor,
    )


def retried_covered_rulings(
    launch_prompt: "LaunchPromptProvider", carried: "LaunchAuthorityTransfer | None"
) -> str | None:
    """The rulings binding a RETRIED tech-lead run, read fresh now: a ruling
    recorded or retired since the original launch binds the retry. Which
    issues is the carried create-once record's (never re-sampled, so a PR whose
    links changed since still binds its issue). Raises as
    :meth:`LaunchPromptProvider.covered_rulings` does."""
    if carried is None:
        return None
    return launch_prompt.covered_rulings(dict(carried.authority.covered_work))
