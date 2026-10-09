"""``release_validated_work``: an operator-approved release of rebuilt work (#9092).

porchpin #262: three validated heads parked ``divergent_validated_heads``.
Their work was rebuilt in PR #479, which was then rebased with conflicts
resolved, so no ancestry proof connects them and #8137's content proof never
releases them. Only the operator can say the work survived. These tests pin
every boundary of that statement: what the tech lead may name, what is bound
at launch, that it never runs unattended, what the proposal shows, and what
the executor re-verifies before the one write it makes.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import (
    CreateTechLeadProposalIssueAction,
    ReleaseValidatedWorkAction,
)
from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.control.tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
)
from issue_orchestrator.control.tech_lead_proposals import (
    build_op_ledger,
    plan_approved_tech_lead_op_executions,
    proposal_ledger_key,
)
from issue_orchestrator.control.tech_lead_reset_retry import STALE_DOWNGRADE_MODE
from issue_orchestrator.control.tech_lead_target_scope import target_scope_violation
from issue_orchestrator.control.tech_lead_validated_work_release import (
    TechLeadValidatedWorkReleaseExecutor,
)
from issue_orchestrator.control.validated_work_recovery_authority import (
    ValidatedWorkRecoveryAuthority,
)
from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
from issue_orchestrator.domain.publication_remote import (
    PublicationPrState,
    PublicationPullRequest,
    PublicationRemoteError,
)
from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdict, ApprovalVerdictKind
from issue_orchestrator.domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
)
from issue_orchestrator.domain.tech_lead_charter import CharterOutcome
from issue_orchestrator.domain.tech_lead_session import (
    ApprovedTechLeadOp,
    StoredTechLeadOp,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.domain.validated_work import (
    LineageRole,
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import (
    AbandonAllOutcome,
    AbandonStatus,
    AbandonValidatedWorkOutcome,
    OperatorResolution,
    ValidatedWorkAuthoritySnapshot,
    ValidatedWorkDisposition,
)
from issue_orchestrator.domain.validated_work_release import (
    ValidatedWorkRelease,
    bind_release,
)
from issue_orchestrator.domain.validated_work_release_intent import (
    MAX_RELEASE_RECORDS,
    ValidatedWorkReleaseIntent,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import Issue
from tests.unit.validated_work_support import DIVERGENT, L, Rig, TIP, capture

REPO = "owner/repo"
ISSUE = 262
BRANCH = "262-cuj-s1-live-seller-pickup-index-in-flight-pickups"
SUPERSEDING = 479
PROPOSAL = 485
SOURCE_RUN = {
    "source_run_id": "run-1",
    "source_session_name": "issue-262",
    "observed_at": "2026-10-08T00:00:00+00:00",
}


def _grant(head: str, *, issue: int = ISSUE, branch: str = BRANCH, revision: int = 0) -> ValidatedWorkAuthoritySnapshot:
    key = ValidatedWorkKey(REPO, issue, branch, head)
    return ValidatedWorkAuthoritySnapshot(
        record_id=key.record_id,
        evidence_id=f"e-{head[:8]}",
        observation_revision=revision,
        validated_head_sha=head,
        branch_name=branch,
        repo_slug=REPO,
        issue_number=issue,
        pr_number=None,
        expected_remote_head_sha=None,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
    )


GRANTS = tuple(sorted((_grant("e5ce8ddd" + "0" * 32), _grant("4e3005a1" + "0" * 32),
                       _grant("20dab383" + "0" * 32)), key=lambda grant: grant.record_id))
RECORD_IDS = tuple(grant.record_id for grant in GRANTS)
RATIONALE = "PR #479 rebuilt slice 2 after rebasing it onto main with conflicts resolved."


def _intent(record_ids=RECORD_IDS, pr: int = SUPERSEDING) -> ValidatedWorkReleaseIntent:
    return ValidatedWorkReleaseIntent(tuple(record_ids), pr)


def _proposed(**overrides) -> ProposedTechLeadAction:
    fields = dict(
        id="A1",
        action_type="release_validated_work",
        target_number=ISSUE,
        body=RATIONALE,
        finding_ids=("T1",),
        release=_intent(),
    )
    fields.update(overrides)
    return ProposedTechLeadAction(**fields)


def _decision(*actions: ProposedTechLeadAction) -> TechLeadDecision:
    return TechLeadDecision(
        summary="summary",
        findings=(TechLeadFinding(id="T1", title="parked", classification="infra",
                                  evidence=("validated-work-release-targets.json",)),),
        proposed_actions=actions,
    )


def _authority(releases=GRANTS) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=ISSUE,
        focus_issue_number=ISSUE,
        observed_validated_work_releases=releases,
    )


def _plan(decision: TechLeadDecision, authority: TechLeadLaunchAuthority | None = None, config: Config | None = None):
    authority = authority or _authority()
    config = config or Config()
    return plan_tech_lead_decision_actions(
        decision,
        config,
        LabelManager(config),
        anchor_issue=Issue(number=ISSUE, title="CUJ S1", labels=[], repo=REPO),
        expected=build_expected_for_mutation(),
        op_ledger={},
        pattern_ledger={},
        observed_session_generation=lambda _n: None,
        bind_validated_work_release=authority.bind_validated_work_release,
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
        **SOURCE_RUN,
    )


# -- the agent's vocabulary -------------------------------------------------


def test_the_decision_parses_a_release_and_requires_it() -> None:
    payload = {
        "summary": "s",
        "findings": [{"id": "T1", "title": "t", "classification": "infra", "evidence": ["x"]}],
        "proposed_actions": [{
            "id": "A1", "action_type": "release_validated_work", "target_number": ISSUE,
            "body": "rebuilt", "finding_ids": ["T1"],
            "release": {"record_ids": list(RECORD_IDS), "superseding_pr_number": SUPERSEDING},
        }],
    }
    (action,) = TechLeadDecision.from_agent_payload(payload).proposed_actions
    assert action.release == _intent()
    assert action.to_dict()["release"] == _intent().to_dict()

    without = dict(payload["proposed_actions"][0])
    del without["release"]
    with pytest.raises(ValueError, match="requires release"):
        TechLeadDecision.from_agent_payload({**payload, "proposed_actions": [without]})


@pytest.mark.parametrize("release, match", [
    ({"record_ids": [], "superseding_pr_number": 479}, "between 1 and"),
    ({"record_ids": ["r1:a", "r1:a"], "superseding_pr_number": 479}, "distinct"),
    ({"record_ids": ["r1:a"], "superseding_pr_number": True}, "positive integer"),
    ({"record_ids": ["r1:a"], "superseding_pr_number": 0}, "positive integer"),
    ({"record_ids": ["r1:a"], "superseding_pr_number": 479, "force": True}, "unexpected fields"),
    ({"record_ids": [f"r1:{n}" for n in range(MAX_RELEASE_RECORDS + 1)], "superseding_pr_number": 479},
     "between 1 and"),
])
def test_a_malformed_release_is_rejected(release, match) -> None:
    with pytest.raises(ValueError, match=match):
        ValidatedWorkReleaseIntent.from_mapping(release, context="A1")


def test_release_is_valid_only_on_its_own_action_type() -> None:
    with pytest.raises(ValueError, match="only valid on release_validated_work"):
        ProposedTechLeadAction(id="A1", action_type="reset_retry", target_number=ISSUE,
                               body="b", release=_intent()).validate()
    with pytest.raises(ValueError, match="not a PR"):
        _proposed(target_is_pr=True).validate()


# -- authority: never unattended --------------------------------------------


def test_the_charter_never_lets_a_release_run_unattended() -> None:
    config = Config()
    assert config.tech_lead.authority.mode_for("release_validated_work") == "propose"
    verdict = TechLeadCharterPolicy.from_config(config).decide("release_validated_work")
    assert verdict.outcome is CharterOutcome.REFUSED_DESTRUCTIVE
    assert verdict.awaits_approval


def test_the_command_cannot_exist_without_an_approved_proposal() -> None:
    release = ValidatedWorkRelease(SUPERSEDING, GRANTS, RATIONALE)
    with pytest.raises(ValueError, match="approved proposal"):
        ReleaseValidatedWorkAction(release=release, proposal_id="A1",
                                   proposal_issue_number=0, expected=build_expected_for_mutation())


def test_planning_refuses_to_execute_a_release_the_policy_somehow_executes(monkeypatch) -> None:
    monkeypatch.setattr(TechLeadCharterPolicy, "decide_for",
                        lambda self, proposed: replace(self.decide("post_comment"), kind=proposed.action_type))
    with pytest.raises(ValueError, match="never executes without approval"):
        _plan(_decision(_proposed()))


# -- launch binding ---------------------------------------------------------


def test_binding_takes_each_named_record_from_the_launch_grant_sorted() -> None:
    named = tuple(reversed(RECORD_IDS[:2]))
    assert _authority().bind_validated_work_release(ISSUE, _intent(named)) == GRANTS[:2]


@pytest.mark.parametrize("record_ids, issue", [
    ((*RECORD_IDS, _grant(TIP).record_id), ISSUE),  # one record never granted
    (RECORD_IDS, 263),  # the grant of another issue
])
def test_binding_refuses_any_record_the_launch_did_not_grant(record_ids, issue) -> None:
    assert _authority().bind_validated_work_release(issue, _intent(record_ids)) is None


def test_the_launch_grant_round_trips_and_is_validated() -> None:
    authority = _authority()
    assert TechLeadLaunchAuthority.from_dict(authority.to_dict()) == authority
    with pytest.raises(ValueError, match="sorted and unique"):
        _authority(tuple(reversed(GRANTS)))
    with pytest.raises(ValueError, match="act-level scope"):
        _authority((_grant(L, issue=263),))


def test_scope_check_names_an_ungranted_record_before_planning() -> None:
    authority = _authority(GRANTS[:2])
    assert target_scope_violation(_decision(_proposed(release=_intent(RECORD_IDS[:2]))), authority) is None
    violation = target_scope_violation(_decision(_proposed()), authority)
    assert violation is not None and "validated-work-release-targets.json" in violation


def test_launch_selector_offers_every_unresolved_record_of_the_issue(tmp_path) -> None:
    store = Rig(tmp_path / "work.sqlite").open()
    first, second = capture(L, issue=42), capture(DIVERGENT, issue=42, run="run-2")
    failed = capture(L, issue=42, branch="other", state=ValidatedWorkState.FAILED,
                     failure=ValidatedWorkFailure.PUSH_FAILED)
    queued = capture(L, issue=43)
    for admission in (first, second, failed, queued):
        store.admit(admission)

    grants = ValidatedWorkRecoveryAuthority(store).release_grants_for((43, 42))

    expected = {first.evidence.record_id, second.evidence.record_id, failed.evidence.record_id}
    assert {grant.record_id for grant in grants} == expected
    assert [grant.record_id for grant in grants] == sorted(expected)
    # Recovery itself never picks between divergent heads; a release may name them.
    assert ValidatedWorkRecoveryAuthority(store).grants_for((42,)) == ()


# -- the proposal -----------------------------------------------------------


def test_a_release_files_a_gated_proposal_showing_every_record_and_the_pr() -> None:
    [planned] = _plan(_decision(_proposed()))

    assert isinstance(planned, CreateTechLeadProposalIssueAction)
    op = planned.op
    assert op.op_type == "release_validated_work"
    assert op.validated_work_release == ValidatedWorkRelease(SUPERSEDING, GRANTS, RATIONALE)
    assert f"issue #{ISSUE} rebuilt elsewhere" in planned.title
    for grant in GRANTS:
        assert grant.record_id in planned.body
        assert grant.validated_head_sha in planned.body
        assert grant.evidence_id in planned.body
    assert f"#{SUPERSEDING}" in planned.body
    assert StoredTechLeadOp.from_dict(op.to_dict()) == op


def test_a_different_release_of_the_same_issue_is_a_different_proposal() -> None:
    [one] = _plan(_decision(_proposed(release=_intent(RECORD_IDS[:1]))))
    [two] = _plan(_decision(_proposed(release=_intent(RECORD_IDS[1:]))))
    ledger = build_op_ledger([(PROPOSAL, one.op)])

    assert proposal_ledger_key(
        "release_validated_work", ISSUE, release=one.op.validated_work_release) in ledger
    assert proposal_ledger_key(
        "release_validated_work", ISSUE, release=two.op.validated_work_release) not in ledger


def test_a_revised_rationale_is_a_different_proposal_never_a_reuse() -> None:
    """Review r2 F2: the rationale is written into every released record, so
    a re-proposal that revises it must not ride the older proposal."""
    [first] = _plan(_decision(_proposed()))
    [revised] = _plan(_decision(_proposed(body="PR #479 rebuilt it; see the conflict notes.")))
    ledger = build_op_ledger([(PROPOSAL, first.op)])

    assert proposal_ledger_key(
        "release_validated_work", ISSUE, release=revised.op.validated_work_release) not in ledger
    assert first.op.validated_work_release != revised.op.validated_work_release
    assert first.op.validated_work_release.rationale == RATIONALE


def test_the_stored_op_carries_a_release_only_for_its_own_type() -> None:
    [planned] = _plan(_decision(_proposed()))
    with pytest.raises(ValueError, match="Only release_validated_work"):
        replace(planned.op, validated_work_release=None)
    with pytest.raises(ValueError, match="Only release_validated_work"):
        replace(planned.op, op_type="reset_retry")
    with pytest.raises(ValueError, match="must match its records"):
        replace(planned.op, target_issue_number=263)


def test_an_approved_proposal_plans_the_bound_release() -> None:
    [planned] = _plan(_decision(_proposed()))
    [action] = plan_approved_tech_lead_op_executions([ApprovedTechLeadOp(PROPOSAL, planned.op)])

    assert isinstance(action, ReleaseValidatedWorkAction)
    assert action.release == planned.op.validated_work_release
    assert action.proposal_issue_number == PROPOSAL
    assert action.rationale == RATIONALE


# -- the executor -----------------------------------------------------------


def _pull(number: int = SUPERSEDING, *, state=PublicationPrState.MERGED, branch: str = f"{BRANCH}-r1",
          head_repo: str = REPO) -> PublicationPullRequest:
    return PublicationPullRequest(number, f"https://github.com/{REPO}/pull/{number}", head_repo, REPO,
                                  branch, "main", "9" * 40, state, f"Closes #{ISSUE}")


class _Pulls:
    def __init__(self, elsewhere=(), own_merged=(), error: Exception | None = None) -> None:
        self.elsewhere, self.own_merged, self.error = tuple(elsewhere), tuple(own_merged), error
        self.requests: list[tuple[str, str]] = []

    def issue_pull_requests(self, request):
        self.requests.append(("issue", request.branch_name))
        if self.error:
            raise self.error
        return self.elsewhere

    def merged_pull_requests(self, request):
        self.requests.append(("merged", request.branch_name))
        return self.own_merged

    def observe(self, request):  # pragma: no cover - the release never reads it
        raise AssertionError("a release reads only the issue's PRs")


class _Grants:
    def __init__(self, current=GRANTS) -> None:
        self.current = tuple(current)

    def grants_for(self, issue_numbers):  # pragma: no cover - recovery only
        raise AssertionError("a release reads release grants")

    def release_grants_for(self, issue_numbers):
        assert tuple(issue_numbers) == (ISSUE,)
        return self.current


def _executor(*, grants=None, pulls=None, outcome=None, events=None, committed=False, approver="operator"):
    calls: list[tuple] = []

    def abandon_all(commands):
        calls.append(commands)
        return outcome if outcome is not None else _committed(commands)

    executor = TechLeadValidatedWorkReleaseExecutor(
        events=events or Mock(),
        grants=grants or _Grants(),
        pull_requests=pulls or _Pulls(elsewhere=(_pull(),)),
        approval=lambda number: ApprovalVerdict(number, ApprovalVerdictKind.MAINTAINER, approver, 7),
        abandon_all=abandon_all,
        committed=lambda _commands: committed,
    )
    return executor, calls


def _action() -> ReleaseValidatedWorkAction:
    return ReleaseValidatedWorkAction(
        release=ValidatedWorkRelease(SUPERSEDING, GRANTS, "PR #479 rebuilt slice 2"),
        proposal_id="A1", finding_ids=("T1",), anchor_issue_number=PROPOSAL,
        proposal_issue_number=PROPOSAL, expected=build_expected_for_mutation())


def _committed(commands) -> AbandonAllOutcome:
    """What the store returns once every command's record is ABANDONED."""
    return AbandonAllOutcome(tuple(
        AbandonValidatedWorkOutcome(
            AbandonStatus.ABANDONED,
            ValidatedWorkDisposition(
                record_id=command.authority.record_id,
                key=ValidatedWorkKey(REPO, ISSUE, command.authority.branch_name,
                                     command.authority.validated_head_sha),
                evidence_id=command.authority.evidence_id,
                state=ValidatedWorkState.ABANDONED,
                lineage_role=LineageRole.DIVERGENT,
                reason=command.reason,
                resolution=OperatorResolution(command.actor, command.reason, SOURCE_RUN["observed_at"]),
            ),
            (), None, "abandoned",
        )
        for command in commands
    ))


def test_an_approved_release_abandons_every_record_naming_approver_and_pr() -> None:
    events = Mock()
    executor, calls = _executor(events=events)

    result = executor.apply(_action())

    assert result.success
    assert result.details["released_record_ids"] == list(RECORD_IDS)
    (commands,) = calls
    assert [command.authority for command in commands] == list(GRANTS)
    for command in commands:
        assert command.actor == f"approved by maintainer @operator on tech-lead proposal #{PROPOSAL}"
        assert f"merged PR #{SUPERSEDING}" in command.reason
        assert "PR #479 rebuilt slice 2" in command.reason
    [event] = [call.args[0] for call in events.publish.call_args_list]
    assert event.event_type is EventName.TECH_LEAD_ACTION_EXECUTED
    assert event.data["boundary"]["record_ids"] == list(RECORD_IDS)


def test_a_superseding_pr_on_a_records_own_branch_is_found_among_its_merged_prs() -> None:
    pulls = _Pulls(elsewhere=(), own_merged=(_pull(branch=BRANCH),))
    executor, calls = _executor(pulls=pulls)

    assert executor.apply(_action()).success
    assert pulls.requests == [("issue", BRANCH), ("merged", BRANCH)]
    assert calls


@pytest.mark.parametrize("pulls, reason", [
    (_Pulls(elsewhere=(_pull(state=PublicationPrState.OPEN),)), "is open, not merged"),
    (_Pulls(elsewhere=(_pull(state=PublicationPrState.CLOSED),)), "is closed, not merged"),
    (_Pulls(elsewhere=(_pull(head_repo="fork/repo"),)), f"is not a PR of {REPO}"),
    (_Pulls(elsewhere=(_pull(480),)), "is not an open or merged PR of issue"),
])
def test_a_pr_that_cannot_have_rebuilt_the_work_closes_the_proposal_unchanged(pulls, reason) -> None:
    executor, calls = _executor(pulls=pulls)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    assert reason in result.details["skip_reason"]
    assert calls == []


def _replayed_by(actor: str):
    def replay(commands):
        return replace(_committed(tuple(replace(c, actor=actor) for c in commands)), replayed=True)
    return replay


def test_a_retry_after_the_release_committed_completes_it_without_any_pr_read() -> None:
    """Review r1 F1 / r2 F1: the records are no longer releasable grants once
    released, and GitHub may be unreadable now; a retry of the committed batch
    completes it from the store's replay, never closing it stale."""
    events = Mock()
    pulls = _Pulls(error=PublicationRemoteError("GitHub is down"))
    executor, _ = _executor(grants=_Grants(()), pulls=pulls, events=events, committed=True)
    executor.abandon_all = _replayed_by("approved by maintainer @alice on tech-lead proposal #485")

    result = executor.apply(_action())

    assert result.success and result.details["replayed"] is True
    assert pulls.requests == []
    [event] = [call.args[0] for call in events.publish.call_args_list]
    assert event.data["boundary"]["replayed"] is True


def test_a_replay_names_who_released_the_records_not_who_reapproved() -> None:
    """Review r2 F3: A approved and the batch committed; B re-approved the
    retry. The records say A released them, and so does the event."""
    events = Mock()
    executor, _ = _executor(events=events, committed=True, approver="bob")
    executor.abandon_all = _replayed_by("approved by maintainer @alice on tech-lead proposal #485")

    executor.apply(_action())

    [event] = [call.args[0] for call in events.publish.call_args_list]
    assert event.data["boundary"]["actor"] == "approved by maintainer @alice on tech-lead proposal #485"


def test_an_unreadable_remote_keeps_the_approved_op_for_a_retry() -> None:
    executor, calls = _executor(pulls=_Pulls(error=PublicationRemoteError("GitHub is down")))

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.FAILURE
    assert result.details.get("mode") != STALE_DOWNGRADE_MODE
    assert calls == []


def test_a_busy_record_defers_and_a_stale_one_closes_the_proposal() -> None:
    busy = AbandonAllOutcome((), AbandonValidatedWorkOutcome(
        AbandonStatus.BUSY, None, (), None, "The retained-work record is already executing"), RECORD_IDS[0])
    executor, _ = _executor(outcome=busy)
    deferred = executor.apply(_action())
    assert deferred.result_type is ActionResultType.FAILURE
    assert deferred.details.get("mode") != STALE_DOWNGRADE_MODE

    stale = AbandonAllOutcome((), AbandonValidatedWorkOutcome(
        AbandonStatus.AUTHORITY_STALE, None, (), GRANTS[0], "changed after confirmation"), RECORD_IDS[0])
    executor, _ = _executor(outcome=stale)
    closed = executor.apply(_action())
    assert closed.details["mode"] == STALE_DOWNGRADE_MODE
    assert "authority_stale" in closed.details["skip_reason"]


def test_reuse_of_an_open_proposal_is_stale_once_a_record_moved() -> None:
    release = ValidatedWorkRelease(SUPERSEDING, GRANTS, RATIONALE)
    executor, _ = _executor()
    assert executor.stale_reason(release) is None
    executor, _ = _executor(grants=_Grants(GRANTS[1:]))
    assert GRANTS[0].record_id in (executor.stale_reason(release) or "")


def test_bind_release_is_the_one_binding_rule() -> None:
    assert bind_release(_intent(tuple(reversed(RECORD_IDS))), issue_number=ISSUE, grants=GRANTS) == GRANTS
    with pytest.raises(ValueError, match="sorted by record id"):
        ValidatedWorkRelease(SUPERSEDING, tuple(reversed(GRANTS)), RATIONALE)
    with pytest.raises(ValueError, match="one issue"):
        ValidatedWorkRelease(SUPERSEDING, tuple(sorted(
            (*GRANTS, _grant(TIP, issue=263)), key=lambda grant: grant.record_id)), RATIONALE)
    with pytest.raises(ValueError, match="rationale"):
        ValidatedWorkRelease(SUPERSEDING, GRANTS, " ")
