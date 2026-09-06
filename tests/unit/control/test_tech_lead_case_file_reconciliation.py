"""Tests for the accumulated-duplicate reconciliation planner (#6989).

The routing fix stops new daily mints; this lane folds the clusters that
accumulated BEFORE it onto their durable case file. It closes issues by number,
so the properties under test are the ones that make that safe: strict plan
validation, one case file per signature, create-once observation identity,
evidence before closure, and a re-run that plans nothing new.
"""

from pathlib import Path

import pytest

from issue_orchestrator.control.actions import (
    Action,
    ActionResult,
    AppendPatternObservationAction,
    CloseIssueAction,
    CreateTechLeadCaseFileIssueAction,
)
from issue_orchestrator.control.tech_lead_case_file_reconciliation import (
    RECONCILIATION_SESSION_NAME,
    CaseFileReconciler,
    CaseFileReconciliationPlan,
)
from issue_orchestrator.control.tech_lead_case_files import (
    CASE_FILE_TITLE_PREFIX,
    build_pattern_ledger,
)
from issue_orchestrator.domain.tech_lead_findings import (
    PatternEvidence,
    pattern_observation_id,
)
from issue_orchestrator.domain.tech_lead_session import TECH_LEAD_OBSERVATION_LABEL
from issue_orchestrator.infra.config import Config

OBSERVED_AT = "2026-08-16T00:00:00+00:00"


def _config() -> Config:
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.filtering.label = "io-scope"
    return config


def _plan_mapping(**overrides):
    mapping = {
        "plan_id": "6989-plan",
        "clusters": [
            {
                "signature": "search-api-budget-exhaustion",
                "tracker": 6928,
                "summary": "Scheduled refresh exhausts the search-API budget.",
                "duplicates": [
                    {"issue": 6966, "note": "Re-verified 2026-08-03."},
                    {"issue": 6977, "note": "Re-verified 2026-08-04."},
                ],
            }
        ],
    }
    mapping.update(overrides)
    return mapping


def _plan(**overrides) -> CaseFileReconciliationPlan:
    return CaseFileReconciliationPlan.from_mapping(_plan_mapping(**overrides))


def _reconciler() -> CaseFileReconciler:
    return CaseFileReconciler(config=_config())


def _ledger(*rows: PatternEvidence):
    return build_pattern_ledger(rows)


def _closed_issue_numbers(actions: list) -> list[int]:
    numbers = []
    for action in actions:
        assert isinstance(action, CloseIssueAction)
        numbers.append(action.issue_number)
    return numbers


# --- plan validation -------------------------------------------------------


def test_plan_parses_clusters_and_duplicates():
    plan = _plan()

    assert plan.plan_id == "6989-plan"
    cluster = plan.clusters[0]
    assert cluster.signature == "search-api-budget-exhaustion"
    assert cluster.tracker_issue_number == 6928
    assert cluster.duplicate_issue_numbers == (6966, 6977)
    assert plan.all_duplicate_issue_numbers == (6966, 6977)


def test_plan_allows_a_registration_only_cluster():
    """A recurring class with nothing accumulated is still worth registering."""
    plan = _plan(
        clusters=[
            {
                "signature": "e2e-suite-chronic-red",
                "tracker": 6918,
                "summary": "The suite is chronically red.",
            }
        ]
    )

    assert plan.clusters[0].duplicates == ()


@pytest.mark.parametrize(
    "mapping, expected",
    [
        ({"plan_id": "", "clusters": []}, "plan_id"),
        ({"plan_id": "p"}, "clusters"),
        ({"plan_id": "p", "clusters": []}, "clusters"),
    ],
)
def test_plan_rejects_missing_required_fields(mapping, expected):
    with pytest.raises(ValueError, match=expected):
        CaseFileReconciliationPlan.from_mapping(mapping)


def test_plan_rejects_unknown_keys():
    """A typo must fail the run, not be silently ignored — this closes issues."""
    mapping = _plan_mapping()
    mapping["clusters"][0]["duplicate"] = [{"issue": 1, "note": "typo"}]

    with pytest.raises(ValueError, match="unknown key"):
        CaseFileReconciliationPlan.from_mapping(mapping)


@pytest.mark.parametrize("value", [0, -3, "6966", True, None])
def test_plan_rejects_non_positive_issue_numbers(value):
    mapping = _plan_mapping()
    mapping["clusters"][0]["duplicates"][0]["issue"] = value

    with pytest.raises(ValueError, match="positive issue number"):
        CaseFileReconciliationPlan.from_mapping(mapping)


def test_plan_rejects_a_signature_used_twice():
    mapping = _plan_mapping()
    mapping["clusters"].append(dict(mapping["clusters"][0], tracker=7000))

    with pytest.raises(ValueError, match="more than one cluster"):
        CaseFileReconciliationPlan.from_mapping(mapping)


def test_plan_rejects_the_same_duplicate_in_two_clusters():
    mapping = _plan_mapping()
    mapping["clusters"].append(
        {
            "signature": "other",
            "tracker": 7000,
            "summary": "Another class.",
            "duplicates": [{"issue": 6966, "note": "already folded elsewhere"}],
        }
    )

    with pytest.raises(ValueError, match="more than once"):
        CaseFileReconciliationPlan.from_mapping(mapping)


def test_plan_rejects_closing_a_tracker():
    """Reconciliation moves trailing evidence; it never closes the work issue."""
    mapping = _plan_mapping()
    mapping["clusters"][0]["duplicates"][0]["issue"] = 6928

    with pytest.raises(ValueError, match="also a cluster's tracker"):
        CaseFileReconciliationPlan.from_mapping(mapping)


# --- phase 1: evidence -----------------------------------------------------


def test_unregistered_signature_opens_one_case_file_carrying_every_sighting():
    actions = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=_ledger(), observed_at=OBSERVED_AT
    )

    assert len(actions) == 1
    creation = actions[0]
    assert isinstance(creation, CreateTechLeadCaseFileIssueAction)
    assert creation.title == f"{CASE_FILE_TITLE_PREFIX}search-api-budget-exhaustion"
    assert TECH_LEAD_OBSERVATION_LABEL in creation.labels
    # Registration + one entry per accumulated duplicate, all on ONE case file.
    assert len(creation.observations) == 3
    assert "#6966" in creation.observations[1].comment
    assert "#6977" in creation.observations[2].comment


def test_registration_establishes_no_classification_or_diagnosis():
    """Backfilled evidence must not make a class promotable (round-1 F1)."""
    actions = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=_ledger(), observed_at=OBSERVED_AT
    )

    creation = actions[0]
    assert isinstance(creation, CreateTechLeadCaseFileIssueAction)
    assert creation.fix_class == ""
    assert creation.area is None
    assert creation.diagnosis == ""


def test_registered_signature_appends_instead_of_opening_a_second_case_file():
    ledger = _ledger(
        PatternEvidence(
            signature="search-api-budget-exhaustion",
            case_file_issue_number=7100,
            observation_count=4,
        )
    )

    actions = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=ledger, observed_at=OBSERVED_AT
    )

    assert len(actions) == 3
    for action in actions:
        assert isinstance(action, AppendPatternObservationAction)
        assert action.issue_number == 7100


def test_appended_classification_stays_empty_for_a_classified_signature():
    """An accrual onto a classified ledger row must not re-state its facts."""
    ledger = _ledger(
        PatternEvidence(
            signature="search-api-budget-exhaustion",
            case_file_issue_number=7100,
            observation_count=4,
            fix_class="code",
            area="tech-lead-pipeline",
            diagnosis="Cache predecessor facts per tick.",
        )
    )

    actions = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=ledger, observed_at=OBSERVED_AT
    )

    append = actions[0]
    assert isinstance(append, AppendPatternObservationAction)
    # The durable row's own facts ride the action (an upgrade or a no-op in the
    # store); the reconciliation contributed none of them.
    assert append.fix_class == "code"
    assert append.area == "tech-lead-pipeline"
    assert append.diagnosis == "Cache predecessor facts per tick."


def test_observation_identity_is_a_pure_function_of_the_plan():
    """Re-running must reproduce the identity so the store skips it."""
    first = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=_ledger(), observed_at=OBSERVED_AT
    )
    later = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=_ledger(), observed_at="2027-01-01T00:00:00+00:00"
    )

    creation = first[0]
    replayed = later[0]
    assert isinstance(creation, CreateTechLeadCaseFileIssueAction)
    assert isinstance(replayed, CreateTechLeadCaseFileIssueAction)
    ids = [observation.observation_id for observation in creation.observations]
    assert ids == [
        observation.observation_id for observation in replayed.observations
    ]
    assert ids[1] == pattern_observation_id(
        source_run_id="6989-plan",
        source_session_name=RECONCILIATION_SESSION_NAME,
        action_id="duplicate-6966",
    )


def test_each_cluster_anchors_on_its_own_tracker():
    plan = _plan(
        clusters=[
            {
                "signature": "e2e-suite-chronic-red",
                "tracker": 6918,
                "summary": "Chronically red.",
                "duplicates": [{"issue": 6961, "note": "Re-verified."}],
            },
            {
                "signature": "stranded-branch-recovery",
                "tracker": 6983,
                "summary": "Five stranded branches.",
                "duplicates": [{"issue": 6959, "note": "Re-verified."}],
            },
        ]
    )

    actions = _reconciler().plan_ledger_actions(
        plan, pattern_ledger=_ledger(), observed_at=OBSERVED_AT
    )

    assert len(actions) == 2
    anchors = []
    for action in actions:
        assert isinstance(action, CreateTechLeadCaseFileIssueAction)
        anchors.append(action.origin.anchor_issue_number)
    assert anchors == [6918, 6983]


# --- phase 2: closure ------------------------------------------------------


def test_open_duplicates_are_closed_with_a_pointer_to_tracker_and_case_file():
    actions = _reconciler().plan_closure_actions(
        _plan(),
        case_file_numbers={"search-api-budget-exhaustion": 7100},
        open_duplicates={6966, 6977},
    )

    assert _closed_issue_numbers(actions) == [6966, 6977]
    closure = actions[0]
    assert isinstance(closure, CloseIssueAction)
    assert "#6928" in closure.comment
    assert "#7100" in closure.comment
    assert "Re-verified 2026-08-03." in closure.comment


def test_already_closed_duplicates_are_not_closed_again():
    actions = _reconciler().plan_closure_actions(
        _plan(),
        case_file_numbers={"search-api-budget-exhaustion": 7100},
        open_duplicates={6977},
    )

    assert _closed_issue_numbers(actions) == [6977]


def test_a_fully_applied_plan_plans_no_closures_on_re_run():
    actions = _reconciler().plan_closure_actions(
        _plan(),
        case_file_numbers={"search-api-budget-exhaustion": 7100},
        open_duplicates=set(),
    )

    assert actions == []


def test_nothing_is_closed_before_its_evidence_has_a_case_file():
    """A failed phase 1 must leave the board untouched, not lose the evidence."""
    actions = _reconciler().plan_closure_actions(
        _plan(), case_file_numbers={}, open_duplicates={6966, 6977}
    )

    assert actions == []


# --- the run: phase ordering and the halt rule -----------------------------


class _FakeHost:
    """A reconciliation boundary that records what the run asked it to do."""

    def __init__(self, *, ledger=(), open_issues=(), fail_evidence=False):
        self._ledger = {row.signature: row for row in ledger}
        self._open = set(open_issues)
        self._fail_evidence = fail_evidence
        self.applied: list[Action] = []
        self.state_reads: list[int] = []

    def pattern_ledger(self):
        return dict(self._ledger)

    def issue_is_open(self, issue_number: int) -> bool:
        self.state_reads.append(issue_number)
        return issue_number in self._open

    def apply(self, actions):
        self.applied.extend(actions)
        results = []
        for action in actions:
            failed = self._fail_evidence and not isinstance(action, CloseIssueAction)
            results.append(
                ActionResult.fail(action, "boom")
                if failed
                else ActionResult.ok(action)
            )
            if isinstance(action, CreateTechLeadCaseFileIssueAction) and not failed:
                # Mirror the applier: a created case file joins the ledger, so
                # the second phase can name it.
                self._ledger[action.pattern_signature] = PatternEvidence(
                    signature=action.pattern_signature,
                    case_file_issue_number=7100,
                    observation_count=len(action.observations),
                )
        return results


def test_dry_run_writes_nothing():
    host = _FakeHost(open_issues={6966, 6977})

    run = _reconciler().run(
        _plan(), host, observed_at=OBSERVED_AT, apply_writes=False
    )

    assert host.applied == []
    assert run.dry_run is True
    assert len(run.evidence.actions) == 1
    # No case file exists yet, so no closure can name one.
    assert run.closure.actions == ()


def test_apply_lands_evidence_then_closes_the_duplicates():
    host = _FakeHost(open_issues={6966, 6977})

    run = _reconciler().run(
        _plan(), host, observed_at=OBSERVED_AT, apply_writes=True
    )

    assert run.ok
    assert isinstance(host.applied[0], CreateTechLeadCaseFileIssueAction)
    # The case file created in phase 1 is what phase 2 closes against.
    assert _closed_issue_numbers(host.applied[1:]) == [6966, 6977]
    assert "#7100" in host.applied[1].comment


def test_failed_evidence_halts_before_any_issue_is_closed():
    host = _FakeHost(open_issues={6966, 6977}, fail_evidence=True)

    run = _reconciler().run(
        _plan(), host, observed_at=OBSERVED_AT, apply_writes=True
    )

    assert run.halted_before_closure is True
    assert run.ok is False
    assert not any(isinstance(action, CloseIssueAction) for action in host.applied)


def test_re_running_an_applied_plan_closes_nothing_again():
    """The duplicates are closed, so phase 2 has nothing left to do."""
    host = _FakeHost(
        ledger=[
            PatternEvidence(
                signature="search-api-budget-exhaustion",
                case_file_issue_number=7100,
                observation_count=3,
            )
        ],
        open_issues=(),
    )

    run = _reconciler().run(
        _plan(), host, observed_at=OBSERVED_AT, apply_writes=True
    )

    assert run.closure.actions == ()
    # Phase 1 still replays its appends; the store skips the ones it recorded.
    assert all(
        isinstance(action, AppendPatternObservationAction) for action in host.applied
    )


def test_no_issue_state_is_read_for_a_cluster_without_a_case_file():
    """GitHub API discipline: never spend a read on an unplannable closure."""
    host = _FakeHost(open_issues={6966, 6977})

    _reconciler().run(_plan(), host, observed_at=OBSERVED_AT, apply_writes=False)

    assert host.state_reads == []


# --- the checked-in plan ---------------------------------------------------


def test_this_repositorys_checked_in_plan_is_valid():
    """The plan closes live issues by number; a broken one must fail here."""
    import yaml

    path = (
        Path(__file__).resolve().parents[3]
        / "repo-specific"
        / "reconciliation"
        / "standing-problems-2026-08.yaml"
    )
    plan = CaseFileReconciliationPlan.from_mapping(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )

    assert {cluster.signature for cluster in plan.clusters} == {
        "search-api-budget-exhaustion",
        "stranded-branch-recovery",
        "e2e-suite-chronic-red",
    }
    # The clusters #6989 names, tracker -> folded duplicates.
    assert {
        cluster.tracker_issue_number: cluster.duplicate_issue_numbers
        for cluster in plan.clusters
    } == {
        6928: (6966, 6977),
        6983: (6959, 6970, 6973),
        6918: (6961, 6978),
    }


# --- the pause gate --------------------------------------------------------


def test_every_planned_mutation_is_fail_closed_on_the_pause_label():
    """A paused issue is one a human is reconciling; this lane must not touch it."""
    from issue_orchestrator.control.reconciliation import get_pause_label

    evidence = _reconciler().plan_ledger_actions(
        _plan(), pattern_ledger=_ledger(), observed_at=OBSERVED_AT
    )
    closures = _reconciler().plan_closure_actions(
        _plan(),
        case_file_numbers={"search-api-budget-exhaustion": 7100},
        open_duplicates={6966, 6977},
    )

    assert evidence and closures
    for action in [*evidence, *closures]:
        assert action.expected is not None, action
        assert get_pause_label() in action.expected.forbidden_labels, action


def test_deferred_closures_name_the_duplicates_awaiting_a_case_file():
    """A first dry run can still show the whole intended outcome."""
    reconciler = _reconciler()

    assert reconciler.deferred_closures(_plan(), case_file_numbers={}) == (6966, 6977)
    assert (
        reconciler.deferred_closures(
            _plan(), case_file_numbers={"search-api-budget-exhaustion": 7100}
        )
        == ()
    )


def test_the_closure_comment_says_reopening_alone_does_not_stick():
    """Reopening without editing the plan is folded again on the next run."""
    closures = _reconciler().plan_closure_actions(
        _plan(),
        case_file_numbers={"search-api-budget-exhaustion": 7100},
        open_duplicates={6966},
    )

    comment = closures[0].comment
    assert "6989-plan" in comment
    assert "remove" in comment.lower()
