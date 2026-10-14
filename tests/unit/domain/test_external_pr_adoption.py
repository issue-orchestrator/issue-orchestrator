"""The external-PR adoption vocabulary's decisions (#9499): pure, so tested directly."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.domain.external_pr_adoption import (
    ORCHESTRATOR_PR_MARKER,
    Admission,
    AdoptedExternalPullRequest,
    AdoptionRefusal,
    ExternalPullRequestFacts,
    ExternalPullRequestLedger,
    IoCustody,
    LinkedIssueFacts,
    PullRequestFacts,
    PullRequestLabelWrites,
    ReportRefusedExternalPullRequest,
    adoption_label_writes,
    effort_label,
    judge_external_pull_request,
    merge_refusal,
    refusal_marker,
    render_refusal_comment,
    return_to_review_label_writes,
    reviewed_head_receipt,
)

IO_BODY = f"Closes #7\n\n*{ORCHESTRATOR_PR_MARKER}*"
EFFORT = "integrate:integration"
NOBODY = IoCustody(working_branch=None, pull_requests=frozenset())

PR = PullRequestFacts(
    number=70, open=True, draft=False, body="Closes #7", branch="7-by-hand", base_branch="integration",
    labels=frozenset(), linked_issue=7,
)
ISSUE = LinkedIssueFacts(number=7, scope_gap=None, labels=frozenset({EFFORT, "agent:backend"}), agent_type="agent:backend")


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
        ExternalPullRequestFacts(pr=replace(PR, linked_issue=None), issue=ISSUE, custody=NOBODY)


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (PR, Admission.ADOPTABLE),
        (replace(PR, labels=frozenset({"origin:external"})), Admission.ADMITTED),
        (replace(PR, body=IO_BODY), Admission.IGNORED),
        # Once adopted, the label decides: io's marker pasted in later does not make it io's.
        (replace(PR, body=IO_BODY, labels=frozenset({"origin:external"})), Admission.ADMITTED),
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
        (replace(PR, linked_issue=None), None, NOBODY, AdoptionRefusal.NO_ISSUE_REFERENCE, "references no issue"),
        (PR, replace(ISSUE, scope_gap="issue is closed"), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT,
         "not open in this engine's scope (issue is closed)"),
        (PR, replace(ISSUE, labels=frozenset({"agent:backend"})), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT,
         f"not labelled `{EFFORT}`"),
        (PR, replace(ISSUE, agent_type=None), NOBODY, AdoptionRefusal.ISSUE_NOT_IN_EFFORT, "no agent label"),
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
        (replace(PR, body=IO_BODY), ISSUE, None),  # io's own PR: its lanes own it
        (replace(PR, labels=frozenset({"origin:external"})), ISSUE, None),
        (PR, ISSUE, "not an adopted PR"),
        (replace(PR, labels=frozenset({"origin:external"}), linked_issue=None), ISSUE, "references no issue, not issue #7"),
        (replace(PR, labels=frozenset({"origin:external"}), linked_issue=8), ISSUE, "references issue #8, not issue #7"),
        (replace(PR, labels=frozenset({"origin:external"})), replace(ISSUE, agent_type=None), "no agent label"),
    ],
)
def test_the_merge_side_re_asks_the_whole_rule_for_exactly_its_issue(
    pr: PullRequestFacts, issue: LinkedIssueFacts, refused: str | None,
) -> None:
    found = merge_refusal("integration", pr, issue=issue, custody=NOBODY)

    assert found is None if refused is None else refused in (found or "")


def test_the_reviewed_head_receipt_names_its_pr_and_exact_head() -> None:
    receipt = reviewed_head_receipt(70, "a" * 40)

    assert receipt.startswith(f"<!-- io:reviewed-head pr=70 sha={'a' * 40} -->")
    assert receipt != reviewed_head_receipt(70, "b" * 40) != reviewed_head_receipt(71, "a" * 40)
    with pytest.raises(ValueError):
        reviewed_head_receipt(70, "abc123")


def test_adoption_enters_the_review_lane_before_its_origin_and_drops_a_foreign_approval() -> None:
    assert adoption_label_writes(["code-reviewed", "bug"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed") == PullRequestLabelWrites(
        remove=("code-reviewed",), add=("needs-code-review", "origin:external"),
    )
    assert adoption_label_writes(["needs-code-review"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed").add == ("origin:external",)
    # Adopted already: nothing to write (a replayed step is a no-op).
    assert adoption_label_writes(["origin:external", "code-reviewed"], review_label="needs-code-review",
                                 reviewed_label="code-reviewed") == PullRequestLabelWrites(remove=(), add=())


def test_a_return_to_review_drops_every_approval_and_re_enters_the_lane() -> None:
    assert return_to_review_label_writes(
        ["origin:external", "code-reviewed", "tech-lead-reviewed"], review_label="needs-code-review",
        approval_labels=("code-reviewed", "tech-lead-reviewed"),
    ) == PullRequestLabelWrites(remove=("code-reviewed", "tech-lead-reviewed"), add=("needs-code-review",))
    assert return_to_review_label_writes(
        ["needs-code-review"], review_label="needs-code-review", approval_labels=("code-reviewed",),
    ) == PullRequestLabelWrites(remove=(), add=())


def _refused(number: int, refusal: AdoptionRefusal) -> ReportRefusedExternalPullRequest:
    return ReportRefusedExternalPullRequest(
        pr_number=number, pr_url="u", issue_number=None, base_branch="integration",
        integration_branch="integration", refusal=refusal, detail="because",
    )


def test_the_ledger_tells_each_refusal_once_per_reason() -> None:
    ledger = ExternalPullRequestLedger()
    first = _refused(80, AdoptionRefusal.NO_ISSUE_REFERENCE)

    assert ledger.record_scan([], [first], {}) == (first,)
    assert ledger.record_scan([], [first], {}) == ()
    changed = _refused(80, AdoptionRefusal.WRONG_BASE)
    assert ledger.record_scan([], [changed], {}) == (changed,)
    # A PR no longer refused is forgotten: refused again later, it is told again.
    assert ledger.record_scan([], [], {}) == ()
    assert ledger.record_scan([], [changed], {}) == (changed,)


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

    assert comment.startswith(refusal_marker(AdoptionRefusal.NO_ISSUE_REFERENCE))
    assert "`integrate:integration`" in comment and "will never merge it" in comment
