"""The external-PR adoption vocabulary's decisions (#9499): pure, so tested directly."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.domain.external_pr_adoption import (
    ORCHESTRATOR_PR_MARKER,
    Admission,
    AdoptedExternalPullRequest,
    AdoptionRefusal,
    Authorship,
    ExternalPullRequestFacts,
    ExternalPullRequestLedger,
    IoCustody,
    IssueLink,
    LabelWrite,
    LaunchAdmission,
    LaunchAuthority,
    LaunchRequest,
    LaunchVerdict,
    LinkedIssueFacts,
    PullRequestFacts,
    PullRequestLabelWrites,
    ReportRefusedExternalPullRequest,
    adoption_label_writes,
    effort_governs,
    effort_label,
    io_owned,
    launch_authority,
    launch_refusal,
    judge_external_pull_request,
    marker_hint,
    merge_refusal,
    refusal_marker,
    render_refusal_comment,
    return_to_review_label_writes,
    reviewed_head_receipt,
)

EFFORT = "integrate:integration"
NOBODY = IoCustody(working_branch=None, pull_requests=frozenset())
NO_LINK = IssueLink(frozenset())

PR = PullRequestFacts(
    number=70, open=True, draft=False, authorship=Authorship.EXTERNAL, branch="7-by-hand", base_branch="integration",
    labels=frozenset(), link=IssueLink(frozenset({7})),
)
ISSUE = LinkedIssueFacts(
    number=7, scope_gap=None, labels=frozenset({EFFORT, "agent:backend"}), agent_type="agent:backend",
    agent_configured=True,
)


def _judge(pr: PullRequestFacts = PR, issue: LinkedIssueFacts | None = ISSUE, custody: IoCustody = NOBODY):
    return judge_external_pull_request("integration", ExternalPullRequestFacts(pr=pr, issue=issue, custody=custody))


def test_the_effort_label_names_its_branch() -> None:
    assert effort_label("tech-lead-improver") == "integrate:tech-lead-improver"
    with pytest.raises(ValueError):
        effort_label("")


def test_facts_read_exactly_the_issue_the_pr_references() -> None:
    with pytest.raises(ValueError):
        ExternalPullRequestFacts(pr=PR, issue=replace(ISSUE, number=8), custody=NOBODY)
    with pytest.raises(ValueError):
        ExternalPullRequestFacts(pr=replace(PR, link=NO_LINK), issue=ISSUE, custody=NOBODY)


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (PR, Admission.ADOPTABLE),
        (replace(PR, labels=frozenset({"origin:external"})), Admission.ADMITTED),
        # io opened it (THE provenance owner's proof, never the body's marker): its lanes own it.
        (replace(PR, authorship=Authorship.IO), Admission.IGNORED),
        # Once adopted, the label decides: not even a PR io opened is io's own then.
        (replace(PR, authorship=Authorship.IO, labels=frozenset({"origin:external"})), Admission.ADMITTED),
        (replace(PR, draft=True), Admission.IGNORED),
        (replace(PR, open=False), Admission.IGNORED),
        (replace(PR, branch="integration", base_branch="main"), Admission.IGNORED),  # the delivery PR
    ],
)
def test_which_prs_are_external_work(pr: PullRequestFacts, expected: Admission) -> None:
    assert _judge(pr).admission is expected


@pytest.mark.parametrize(
    ("pr", "issue", "custody", "refusal", "detail"),
    [
        (replace(PR, link=NO_LINK), None, NOBODY, AdoptionRefusal.NO_ISSUE_REFERENCE, "references no issue"),
        (replace(PR, link=IssueLink(frozenset({7, 8}))), None, NOBODY, AdoptionRefusal.AMBIGUOUS_ISSUE_REFERENCE,
         "references issues #7 and #8; io adopts a PR for exactly one issue"),
        (PR, replace(ISSUE, scope_gap="issue is closed"), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT,
         "not open in this engine's scope (issue is closed)"),
        (PR, replace(ISSUE, labels=frozenset({"agent:backend"})), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT,
         f"not labelled `{EFFORT}`"),
        (PR, replace(ISSUE, agent_type=None, agent_configured=False), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT,
         "no agent label"),
        (PR, replace(ISSUE, agent_type="agent:unknown", agent_configured=False), NOBODY,
         AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "`agent:unknown` names no agent this engine runs"),
        (replace(PR, base_branch="main"), ISSUE, NOBODY, AdoptionRefusal.WRONG_BASE, "targets `main`"),
        (PR, ISSUE, IoCustody("7-io", frozenset()), AdoptionRefusal.IO_WORK_IN_FLIGHT, "own branch `7-io`"),
        (PR, ISSUE, IoCustody(None, frozenset({69, 70})), AdoptionRefusal.IO_WORK_IN_FLIGHT, "PR #69"),
    ],
)
def test_the_refusals(
    pr: PullRequestFacts, issue: LinkedIssueFacts | None, custody: IoCustody, refusal: AdoptionRefusal, detail: str,
) -> None:
    verdict = _judge(pr, issue, custody)

    assert (verdict.admission, verdict.refusal) == (Admission.REFUSED, refusal)
    assert detail in verdict.detail


def test_io_s_own_hold_on_the_pr_itself_is_no_conflict() -> None:
    adopted = replace(PR, labels=frozenset({"origin:external"}))

    assert _judge(adopted, custody=IoCustody("7-by-hand", frozenset({70}))).admission is Admission.ADMITTED


def test_a_pr_into_another_base_for_an_issue_outside_the_effort_is_not_io_s_business() -> None:
    elsewhere = replace(PR, base_branch="main")

    assert _judge(elsewhere, replace(ISSUE, labels=frozenset({"agent:backend"}))).admission is Admission.IGNORED


@pytest.mark.parametrize(
    ("pr", "issue", "refused"),
    [
        (replace(PR, authorship=Authorship.IO), ISSUE, None),  # io's own PR: its lanes own it
        (replace(PR, labels=frozenset({"origin:external"})), ISSUE, None),
        (PR, ISSUE, "not an adopted PR"),
        (replace(PR, labels=frozenset({"origin:external"}), link=NO_LINK), ISSUE, "references no issue, not issue #7"),
        (replace(PR, labels=frozenset({"origin:external"}), link=IssueLink(frozenset({8}))), ISSUE,
         "references issue #8, not issue #7"),
        (replace(PR, labels=frozenset({"origin:external"}), link=IssueLink(frozenset({7, 8}))), ISSUE,
         "references issues #7 and #8, not issue #7"),
        (replace(PR, labels=frozenset({"origin:external"})), replace(ISSUE, agent_type=None, agent_configured=False),
         "no agent label"),
        (replace(PR, labels=frozenset({"origin:external"})), replace(ISSUE, agent_configured=False),
         "names no agent this engine runs"),
    ],
)
def test_the_merge_side_re_asks_the_whole_rule_for_exactly_its_issue(
    pr: PullRequestFacts, issue: LinkedIssueFacts, refused: str | None,
) -> None:
    found = merge_refusal("integration", pr, issue=issue, custody=NOBODY)

    assert found is None if refused is None else refused in (found or "")


def test_an_issue_link_names_none_one_or_several_issues() -> None:
    assert (NO_LINK.issue, NO_LINK.ambiguous, NO_LINK.describe()) == (None, False, "no issue")
    one = IssueLink(frozenset({7}))
    assert (one.issue, one.ambiguous, one.describe()) == (7, False, "issue #7")
    several = IssueLink(frozenset({9, 7, 8}))
    assert (several.issue, several.ambiguous, several.describe()) == (None, True, "issues #7, #8 and #9")


@pytest.mark.parametrize(
    ("pr", "governed"),
    [
        (PR, True),  # external, into the effort branch
        (replace(PR, base_branch="main"), False),  # external, elsewhere: not the effort's business
        (replace(PR, base_branch="main", labels=frozenset({"origin:external"})), True),  # adopted, then retargeted
        (replace(PR, authorship=Authorship.IO), False),  # io's own
    ],
)
def test_which_prs_the_effort_governs(pr: PullRequestFacts, governed: bool) -> None:
    assert effort_governs("integration", pr) is governed


ADOPTED = replace(PR, labels=frozenset({"origin:external"}))
QUEUED = LaunchAuthority(effort="integration", issue_number=7, pr_number=70, agent="agent:backend")


def _launch(request: LaunchRequest, pr: PullRequestFacts = ADOPTED, issue: LinkedIssueFacts = ISSUE) -> str | None:
    return launch_refusal("integration", request, pr, issue=issue, custody=NOBODY)


def _request(authority: LaunchAuthority | None = QUEUED, **overrides: object) -> LaunchRequest:
    from issue_orchestrator.domain.session_kind import SessionKind

    fields: dict[str, object] = {"kind": SessionKind.REWORK, "issue_number": 7, "pr_number": 70,
                                 "authority": authority, "agent": None}
    fields.update(overrides)
    return LaunchRequest(**fields)  # type: ignore[arg-type]


def test_queued_work_launches_against_its_authority_unchanged() -> None:
    assert _launch(_request()) is None
    assert "queued under the `other` effort" in (_launch(_request(replace(QUEUED, effort="other"))) or "")
    assert "queued for PR #71" in (_launch(_request(replace(QUEUED, pr_number=71))) or "")
    assert "names `agent:backend` now, not `agent:frontend`" in (
        _launch(_request(replace(QUEUED, agent="agent:frontend"))) or "")


def test_an_authority_keeps_custody_whatever_the_pr_says_now() -> None:
    # Origin label dropped and retargeted: the PR's facts say "not governed".
    elsewhere = replace(PR, base_branch="main")
    assert _launch(_request(None), elsewhere) is None
    assert _launch(_request(), elsewhere) is not None


def test_a_pr_found_at_launch_is_never_one_the_effort_governs() -> None:
    assert "found at launch" in (_launch(_request(None, resolved_at_launch=True)) or "")
    assert _launch(_request(None, resolved_at_launch=True), replace(PR, authorship=Authorship.IO)) is None


def test_the_authority_is_built_from_exactly_the_admitted_pr_and_issue() -> None:
    assert launch_authority("integration", ADOPTED, ISSUE) == QUEUED
    assert launch_authority("integration", replace(PR, authorship=Authorship.IO), ISSUE) is None


def test_once_adopted_the_origin_label_decides_provenance() -> None:
    assert io_owned(Authorship.IO, ["needs-code-review"])
    assert not io_owned(Authorship.IO, ["origin:external"])
    assert not io_owned(Authorship.EXTERNAL, [])


def test_io_s_marker_is_only_a_hint() -> None:
    """The body is its author's to write: the marker says where to look for
    io's own PRs, and THE provenance owner proves them (#9499 review F1)."""
    assert marker_hint(f"Closes #7\n\n*{ORCHESTRATOR_PR_MARKER}*")
    assert not marker_hint(None) and not marker_hint("Closes #7")


def test_a_refused_launch_says_why_and_an_admitted_one_does_not() -> None:
    with pytest.raises(ValueError):
        LaunchAdmission(LaunchVerdict.WITHDRAWN)
    with pytest.raises(ValueError):
        LaunchAdmission(LaunchVerdict.ADMITTED, "because")


def test_the_reviewed_head_receipt_names_its_pr_and_exact_head() -> None:
    receipt = reviewed_head_receipt(70, "a" * 40)

    assert receipt.startswith(f"<!-- io:reviewed-head pr=70 sha={'a' * 40} -->")
    assert receipt != reviewed_head_receipt(70, "b" * 40) != reviewed_head_receipt(71, "a" * 40)
    with pytest.raises(ValueError):
        reviewed_head_receipt(70, "abc123")


APPROVALS = ("code-reviewed", "tech-lead-reviewed")


def _writes(*steps: tuple[str, bool]) -> PullRequestLabelWrites:
    return PullRequestLabelWrites(tuple(LabelWrite(label, add) for label, add in steps))


def test_adoption_drops_foreign_approvals_enters_the_review_lane_and_marks_its_origin_last() -> None:
    assert adoption_label_writes(["code-reviewed", "tech-lead-reviewed", "bug"], review_label="needs-code-review",
                                 approvals=APPROVALS) == _writes(
        ("code-reviewed", False), ("tech-lead-reviewed", False), ("needs-code-review", True),
        ("origin:external", True),
    )
    # A prefix already applied is not written again.
    assert adoption_label_writes(["needs-code-review"], review_label="needs-code-review",
                                 approvals=APPROVALS) == _writes(("origin:external", True))
    # Adopted already: nothing to write (a replayed step is a no-op).
    assert adoption_label_writes(["origin:external", "code-reviewed"], review_label="needs-code-review",
                                 approvals=APPROVALS) == _writes()


def test_a_return_to_review_drops_its_routing_approval_last() -> None:
    """Every prefix keeps ``code-reviewed`` until the review label is on, so a
    write failing part-way is planned again, never stranded outside every lane."""
    assert return_to_review_label_writes(
        ["origin:external", "code-reviewed", "tech-lead-reviewed"], review_label="needs-code-review",
        reviewed_label="code-reviewed", other_approvals=("tech-lead-reviewed",),
    ) == _writes(("tech-lead-reviewed", False), ("needs-code-review", True), ("code-reviewed", False))
    assert return_to_review_label_writes(
        ["needs-code-review", "code-reviewed"], review_label="needs-code-review", reviewed_label="code-reviewed",
        other_approvals=("code-reviewed",),  # tech-lead-reviewed configured as code-reviewed itself
    ) == _writes(("code-reviewed", False))
    assert return_to_review_label_writes(
        ["needs-code-review"], review_label="needs-code-review", reviewed_label="code-reviewed",
        other_approvals=("tech-lead-reviewed",),
    ) == _writes()


def _refused(number: int, refusal: AdoptionRefusal) -> ReportRefusedExternalPullRequest:
    return ReportRefusedExternalPullRequest(
        pr_number=number, pr_url="u", issue_number=None, base_branch="integration",
        integration_branch="integration", refusal=refusal, detail="because",
    )


def test_the_ledger_acknowledges_a_refusal_only_once_its_report_is_seen() -> None:
    ledger = ExternalPullRequestLedger()
    first = _refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE)

    assert ledger.record_scan([], [first], {}) == (first,)
    # Planned is not told: until the report is acknowledged, every scan returns it.
    assert ledger.record_scan([], [first], {}) == (first,)
    ledger.acknowledge(first)
    assert ledger.record_scan([], [first], {}) == ()
    changed = _refused(80, AdoptionRefusal.WRONG_BASE)
    assert ledger.record_scan([], [changed], {}) == (changed,)
    ledger.acknowledge(changed)
    # A PR no longer refused that way is forgotten: refused again later, it is returned again.
    assert ledger.record_scan([], [], {}) == ()
    assert ledger.record_scan([], [changed], {}) == (changed,)


def test_a_refusal_s_identity_is_its_reason_issue_bases_and_detail() -> None:
    step = replace(_refused(80, AdoptionRefusal.ISSUE_NOT_IN_EFFORT), issue_number=231, detail="issue #231 closed")

    assert refusal_marker(step) == refusal_marker(replace(step, pr_url="another url"))
    for changed in (
        replace(step, issue_number=232), replace(step, detail="issue #232 closed"), replace(step, base_branch="main"),
        replace(step, integration_branch="other"), replace(step, refusal=AdoptionRefusal.WRONG_BASE),
    ):
        assert refusal_marker(changed) != refusal_marker(step)


def test_the_ledger_keeps_found_receipts_only_for_adopted_prs() -> None:
    ledger = ExternalPullRequestLedger()
    ledger.remember_reviewed_head(70, "a" * 40)
    ledger.remember_reviewed_head(71, "b" * 40)

    ledger.record_scan([AdoptedExternalPullRequest(pr_number=70, issue_number=7, pr_url="u")], [], {7: frozenset({70})})

    assert ledger.reviewed_head_known(70, "a" * 40) and not ledger.reviewed_head_known(70, "b" * 40)
    assert not ledger.reviewed_head_known(71, "b" * 40)  # no longer adopted: forgotten
    assert ledger.attempts_at(7) == frozenset({70}) and ledger.attempts_at(8) == frozenset()


def test_a_refusal_comment_carries_its_reason_marker_and_the_remedy() -> None:
    comment = render_refusal_comment(_refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE))

    assert comment.startswith(refusal_marker(_refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE)))
    assert "`integrate:integration`" in comment and "will never merge it" in comment
