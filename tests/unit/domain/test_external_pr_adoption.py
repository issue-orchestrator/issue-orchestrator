"""The external-PR adoption vocabulary's decisions (#9499): pure, so tested directly."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.external_pr_adoption import (
    ORCHESTRATOR_PR_MARKER,
    AdoptExternalPullRequest,
    AdoptionLabelWrites,
    AdoptionRefusal,
    ExternalPullRequestLedger,
    ExternalStanding,
    MergeProvenance,
    ReportRefusedExternalPullRequest,
    adoption_label_writes,
    adoption_obstacle,
    effort_gap,
    effort_label,
    external_standing,
    merge_provenance,
    refusal_marker,
    render_refusal_comment,
)

IO_BODY = f"Closes #1\n\n*{ORCHESTRATOR_PR_MARKER}*"
EFFORT = "integrate:integration"


def test_the_effort_label_names_its_branch() -> None:
    assert effort_label("tech-lead-improver") == "integrate:tech-lead-improver"
    with pytest.raises(ValueError):
        effort_label("")


@pytest.mark.parametrize(
    ("body", "pr_labels", "issue_labels", "expected"),
    [
        (IO_BODY, [], [], MergeProvenance.IO_AUTHORED),
        ("Closes #1", [], [EFFORT], MergeProvenance.NOT_ADOPTED),
        ("Closes #1", ["origin:external"], [EFFORT], MergeProvenance.ADOPTED),
        ("Closes #1", ["origin:external"], ["integrate:another"], MergeProvenance.LEFT_EFFORT),
    ],
)
def test_merge_provenance_admits_only_io_s_own_and_adopted_work(
    body: str, pr_labels: list[str], issue_labels: list[str], expected: MergeProvenance,
) -> None:
    provenance = merge_provenance(branch="integration", pr_body=body, pr_labels=pr_labels, issue_labels=issue_labels)

    assert provenance is expected
    assert provenance.admitted is (expected in (MergeProvenance.IO_AUTHORED, MergeProvenance.ADOPTED))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ExternalStanding.CANDIDATE),
        ({"pr_body": IO_BODY}, ExternalStanding.NOT_EXTERNAL),
        ({"draft": True}, ExternalStanding.NOT_EXTERNAL),
        ({"pr_branch": "integration", "base_branch": "main"}, ExternalStanding.NOT_EXTERNAL),  # the delivery PR
        ({"base_branch": "main"}, ExternalStanding.OTHER_BASE),
        ({"base_branch": None}, ExternalStanding.OTHER_BASE),
        ({"pr_labels": ["origin:external"]}, ExternalStanding.ADOPTED),
    ],
)
def test_external_standing(overrides: dict[str, object], expected: ExternalStanding) -> None:
    facts: dict[str, object] = {
        "branch": "integration", "pr_body": "Closes #1", "pr_branch": "1-by-hand", "base_branch": "integration",
        "draft": False, "pr_labels": [],
    }
    facts.update(overrides)

    assert external_standing(**facts) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("labels", "agent", "gap"),
    [
        ([EFFORT, "agent:backend"], "agent:backend", None),
        (["agent:backend"], "agent:backend", "not labelled `integrate:integration`"),
        ([EFFORT], None, "no agent label"),
    ],
)
def test_effort_gap(labels: list[str], agent: str | None, gap: str | None) -> None:
    found = effort_gap(branch="integration", issue_number=7, issue_labels=labels, agent_type=agent)

    assert found is None if gap is None else gap in (found or "")


def test_adoption_enters_the_review_lane_before_its_origin_and_drops_a_foreign_approval() -> None:
    assert adoption_label_writes(["code-reviewed", "bug"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed") == AdoptionLabelWrites(
        remove=("code-reviewed",), add=("needs-code-review", "origin:external"),
    )
    assert adoption_label_writes(["needs-code-review"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed").add == ("origin:external",)
    # Adopted already: nothing to write (a replayed step is a no-op).
    assert adoption_label_writes(["origin:external", "code-reviewed"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed") == AdoptionLabelWrites(remove=(), add=())


_STEP = AdoptExternalPullRequest(
    issue_number=7, pr_number=70, pr_url="u", pr_title="t", integration_branch="integration",
    review_label="needs-code-review",
)


@pytest.mark.parametrize(
    ("pr_open", "base", "body", "issue_labels", "obstacle"),
    [
        (True, "integration", "Closes #7", [EFFORT], None),
        (False, "integration", "Closes #7", [EFFORT], "no longer open"),
        (True, "main", "Closes #7", [EFFORT], "now targets 'main'"),
        (True, "integration", IO_BODY, [EFFORT], "io's own"),
        (True, "integration", "Closes #7", [], "left the integration effort"),
    ],
)
def test_an_adoption_is_written_only_on_what_github_says_now(
    pr_open: bool, base: str, body: str, issue_labels: list[str], obstacle: str | None,
) -> None:
    found = adoption_obstacle(_STEP, pr_open=pr_open, base_branch=base, pr_body=body, issue_labels=issue_labels)

    assert found is None if obstacle is None else obstacle in (found or "")


def _refused(number: int, refusal: AdoptionRefusal) -> ReportRefusedExternalPullRequest:
    return ReportRefusedExternalPullRequest(
        pr_number=number, pr_url="u", issue_number=None, base_branch="integration",
        integration_branch="integration", refusal=refusal, detail="because",
    )


def test_the_ledger_tells_each_refusal_once_per_reason() -> None:
    ledger = ExternalPullRequestLedger()
    first = _refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE)

    assert ledger.record_scan([], [first]) == (first,)
    assert ledger.record_scan([], [first]) == ()
    changed = _refused(80, AdoptionRefusal.WRONG_BASE)
    assert ledger.record_scan([], [changed]) == (changed,)
    # A PR no longer refused is forgotten: refused again later, it is told again.
    assert ledger.record_scan([], []) == ()
    assert ledger.record_scan([], [changed]) == (changed,)


def test_a_refusal_comment_carries_its_reason_marker_and_the_remedy() -> None:
    comment = render_refusal_comment(_refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE))

    assert comment.startswith(refusal_marker(AdoptionRefusal.NO_ISSUE_REFERENCE))
    assert "`integrate:integration`" in comment and "will never merge it" in comment
