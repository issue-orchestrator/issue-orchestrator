"""The improver findings validator: every Field rule of the prompt, one case each (#7490).

Each case starts from a VALID per-output example (``examples/improver/findings``)
and breaks exactly one thing. It asserts the rejection names that case's rule,
so deleting the rule's check turns the case red; ``test_every_rule_has_a_case``
keeps the list whole.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    StagedEvidence,
    validate_findings,
)
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from tests.unit.improver_support import (
    A1_FIRST_SEEN,
    CUTOFF,
    EXAMPLE_NAMES,
    EXAMPLES,
    OPEN_TRACKER,
    build_improver_data,
    example,
)

Doc = dict
Mutation = Callable[[Doc], None]


@pytest.fixture(scope="module")
def evidence(tmp_path_factory: pytest.TempPathFactory) -> StagedEvidence:
    return load_staged_evidence(build_improver_data(tmp_path_factory.mktemp("improver")))


def _finding(doc: Doc) -> dict:
    return doc["findings"][0]


def _rules(doc: Doc, evidence: StagedEvidence) -> frozenset[Rule]:
    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)
    return rejected.value.rules


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_every_per_output_example_is_valid(name: str, evidence: StagedEvidence) -> None:
    findings = validate_findings((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"), evidence)

    assert [f.output for f in findings.findings] == [name]


def test_the_examples_cover_every_output_kind() -> None:
    from typing import get_args

    from issue_orchestrator.contracts.improver_findings import Output

    assert set(EXAMPLE_NAMES) == set(get_args(Output))
    assert {p.stem for p in EXAMPLES.glob("*.json")} == set(EXAMPLE_NAMES)


def _set(path: str, value: object) -> Mutation:
    """Set ``findings[0].<path>`` (dotted; integers index lists)."""

    def mutate(doc: Doc) -> None:
        node: object = _finding(doc)
        *parents, last = path.split(".")
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]  # type: ignore[index]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value  # type: ignore[index]

    return mutate


def _drop(path: str) -> Mutation:
    def mutate(doc: Doc) -> None:
        node: object = _finding(doc)
        *parents, last = path.split(".")
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]  # type: ignore[index]
        del node[last]  # type: ignore[union-attr]

    return mutate


def _all(*mutations: Mutation) -> Mutation:
    def mutate(doc: Doc) -> None:
        for m in mutations:
            m(doc)

    return mutate


def _top(key: str, value: object) -> Mutation:
    def mutate(doc: Doc) -> None:
        doc[key] = value

    return mutate


def _duplicate_finding(doc: Doc) -> None:
    doc["findings"].append(json.loads(json.dumps(_finding(doc))))


def _second_finding_same_case(doc: Doc) -> None:
    twin = json.loads(json.dumps(_finding(doc)))
    twin["id"] = "another-id"
    doc["findings"].append(twin)


def _append_observed(entry: dict) -> Mutation:
    def mutate(doc: Doc) -> None:
        _finding(doc)["observed"].append(entry)

    return mutate


def _drop_observed(index: int) -> Mutation:
    def mutate(doc: Doc) -> None:
        del _finding(doc)["observed"][index]

    return mutate


PRE_START_LOG = {
    "at": "2026-09-27T18:05:00+00:00", "kind": "occurrence",
    "source": "audit.json#/no_progress/log_signatures/0/first_seen", "supports": "origin",
}

#: (rule, example it starts from, the one thing broken). Several rules have
#: more than one way to break them; each way is its own case.
CASES: list[tuple[Rule, str, Mutation]] = [
    # the file's shape
    (Rule.SCHEMA, "exam_case", _set("present_after_start", True)),
    (Rule.SCHEMA, "exam_case", _set("surprise", "field")),
    (Rule.SCHEMA, "exam_case", _set("tracked_issue", "7491")),
    (Rule.SCHEMA, "exam_case", _set("grading_window.from", "2026-09-27T09:00:00")),
    (Rule.ENGINE_IDENTITY, "exam_case", _top("engine_commit", "f" * 40)),
    (Rule.ENGINE_IDENTITY, "exam_case", _top("engine_started_at", "2026-09-28T11:00:00+00:00")),
    (Rule.UNIQUE_FINDING_IDS, "capability_issue", _duplicate_finding),
    # anomaly keys and classification
    (Rule.ANOMALY_KEY_EXISTS, "exam_case", _set("anomaly_keys.0.subject", "#999")),
    (Rule.TRACKED_ISSUE_OPEN, "prompt_proposal", _set("tracked_issue", 1)),
    (Rule.TRACKED_ISSUE_OPEN, "prompt_proposal", _drop("tracked_issue")),
    (Rule.NEW_DEFECT_UNTRACKED, "exam_case", _set("tracked_issue", OPEN_TRACKER)),
    (Rule.UNKNOWN_CLASSIFICATION_INVESTIGATES, "exam_case", _set("classification", "unknown")),
    (Rule.UNKNOWN_CLASSIFICATION_INVESTIGATES, "needs_investigation", _set("tracked_issue", OPEN_TRACKER)),
    # liveness
    (Rule.UNKNOWN_LIVENESS_INVESTIGATES, "capability_issue", _set("present_after_start", "unknown")),
    (Rule.NO_PURE_HISTORY, "needs_investigation",
     _all(_set("present_after_start", "false"), _set("recurs_after_start", "false"),
          _set("anomaly_keys.0", {"kind": "no_progress_log", "subject": "#410",
                                  "signature": "WARNING io.retry: validation retry refused"}))),
    (Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "needs_investigation", _set("present_after_start", "false")),
    (Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "capability_issue",
     _set("observed.0", {"at": "2026-09-28T10:00:00+00:00", "kind": "occurrence",
                         "source": "audit.json#/action_liveness/parked/0/last_failed_at",
                         "supports": "origin"})),
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "capability_issue", _set("recurs_after_start", "true")),
    # "Does not recur" while the staged records show a post-start occurrence (r1 F2).
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "charter_proposal", _set("recurs_after_start", "false")),
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "exam_case",
     _append_observed({**PRE_START_LOG, "supports": "recurs_after_start"})),
    (Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, "exam_case", _set("origin", "unknown")),
    (Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, "charter_proposal", _set("origin", "before_start")),
    (Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, "charter_proposal", _set("observed.1.supports", "origin")),
    # Citing only the post-start sighting hides a pre-start one the records show (r2 F1).
    (Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, "exam_case",
     _all(_drop_observed(2), _set("origin", "unknown"), _set("grading_window.from", "unknown"))),
    # Someone else's decision is not this anomaly noticed (r2 F2).
    (Rule.STALL_EVIDENCE_ABOUT_THE_ANOMALY, "exam_case", _set("stall_evidence", ["D2"])),
    # A source file shows no notice (r4 F1).
    (Rule.NOTICED_CITES_A_NOTICE, "exam_case",
     _set("stall_evidence", ["engine-source:src/issue_orchestrator/domain/tech_lead_charter.py"])),
    # citations
    (Rule.CITATION_RESOLVES, "exam_case", _set("observed.0.source", "audit.json#/anomalies/99")),
    (Rule.CITATION_RESOLVES, "exam_case", _set("observed.0.source", "unstaged.json#/anomalies/0")),
    (Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, "exam_case", _set("observed.0.supports", "recurs_after_start")),
    (Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, "exam_case", _set("observed.0.at", "2026-09-28T17:59:00+00:00")),
    (Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, "exam_case", _set("observed.0.source", "audit-previous.json#/anomalies/0")),
    # A snapshot of ANOTHER anomalies' record, or of no anomaly at all (r1 F1).
    (Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, "charter_proposal", _set("observed.0.source", "audit.json#/anomalies/0")),
    (Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, "charter_proposal", _set("observed.0.source", "audit.json#/generated_at")),
    (Rule.OCCURRENCE_IS_A_DATED_RECORD, "exam_case", _set("observed.1.at", "2026-09-28T17:31:00+00:00")),
    (Rule.OCCURRENCE_IS_A_DATED_RECORD, "exam_case", _set("observed.1.source", "audit.json#/no_progress/window_end")),
    (Rule.OCCURRENCE_IS_A_DATED_RECORD, "exam_case",
     _set("observed.1.source", "audit.json#/no_progress/log_signatures/1/last_seen")),
    (Rule.OCCURRENCE_IS_A_DATED_RECORD, "exam_case", _set("observed.1.supports", "present_after_start")),
    # The whole record may be cited, but only at one of its own dates.
    (Rule.OCCURRENCE_IS_A_DATED_RECORD, "exam_case",
     _all(_set("observed.1.source", "audit.json#/no_progress/log_signatures/0"),
          _set("observed.1.at", "2026-09-28T17:31:00+00:00"))),
    # the grading window
    (Rule.WINDOW_ENDS_AT_AUDIT_CUTOFF, "exam_case", _set("grading_window.to", "2026-09-28T19:00:00+00:00")),
    (Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, "exam_case", _set("grading_window.from", "2026-09-28T17:30:00+00:00")),
    # Earlier than every occurrence, but no cited occurrence is dated then.
    (Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, "charter_proposal", _set("grading_window.from", "2026-09-28T12:30:00+00:00")),
    (Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, "exam_case", _set("grading_window.from", "2026-09-28T18:00:00+00:00")),
    (Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, "exam_case",
     # The current audit's first_seen is cited, but the previous audit saw it earlier.
     _all(_set("observed.2", PRE_START_LOG), _set("grading_window.from", PRE_START_LOG["at"]))),
    # stall points
    (Rule.STALL_EVIDENCE_REQUIRED, "exam_case", _set("stall_evidence", [])),
    (Rule.STALL_EVIDENCE_RESOLVES, "exam_case", _set("stall_evidence", ["D404"])),
    (Rule.STALL_EVIDENCE_RESOLVES, "prompt_proposal",
     _set("stall_evidence", ["charter.json#/actions/no_such_kind/outcome"])),
    (Rule.STALL_EVIDENCE_RESOLVES, "prompt_proposal",
     _set("stall_evidence", ["engine-source:src/issue_orchestrator/nope.py"])),
    (Rule.NOT_NOTICED_NEEDS_PROVEN_ONSET, "charter_proposal", _set("grading_window.from", "unknown")),
    (Rule.NOT_NOTICED_NEEDS_PROVEN_ONSET, "capability_issue",
     # A parked action's last failure is dated, but it is not a first occurrence.
     _all(_set("stall_point", "not_noticed"), _set("stall_evidence", []))),
    (Rule.NOT_NOTICED_CITES_NO_NOTICE, "charter_proposal", _set("stall_evidence", ["D1"])),
    (Rule.ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION, "capability_issue", _set("stall_evidence", ["D1"])),
    (Rule.ACTED_NOT_EFFECTIVE_NEEDS_LATER_OBSERVATION, "capability_issue",
     # Only the pre-application occurrence is cited: nothing shows it persisted.
     _all(_set("present_after_start", "unknown"), _set("recurs_after_start", "true"),
          _set("observed.0", {"at": "2026-09-28T10:00:00+00:00", "kind": "occurrence",
                              "source": "audit.json#/action_liveness/parked/0/last_failed_at",
                              "supports": "recurs_after_start"}))),
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal", _set("stall_evidence", ["D1"])),
    # A setting that resolves but restricts nothing shows nothing out of charter (r7 F3).
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _set("stall_evidence", ["D1", "charter.json#/roles/flow/enabled"])),
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _set("stall_evidence", ["D1", "charter.json#/actions/post_comment/outcome"])),
    # outputs
    (Rule.INVESTIGATION_SHAPE, "needs_investigation", _set("missing_evidence", [])),
    (Rule.INVESTIGATION_SHAPE, "needs_investigation", _set("proposal", "do something")),
    (Rule.REMEDY_SHAPE, "capability_issue", _drop("proposal")),
    (Rule.REMEDY_SHAPE, "capability_issue", _drop("root_cause")),
    (Rule.EXAM_CASE_NEEDS_EXAM_REPRODUCTION, "capability_issue", _set("output", "exam_case")),
    (Rule.CASE_ID_ONLY_FOR_EXAM_REPRODUCTION, "capability_issue", _set("reproduction.case_id", "E-new")),
    (Rule.CASE_ID_ONLY_FOR_EXAM_REPRODUCTION, "exam_case", _drop("reproduction.case_id")),
    (Rule.EXAM_CASE_ID_IS_NEW, "exam_case", _set("reproduction.case_id", "A-halted-exchange-validated-work")),
    (Rule.EXAM_CASE_ID_IS_NEW, "exam_case", _second_finding_same_case),
    (Rule.REPRODUCTION_FAILS_ON_ENGINE_COMMIT, "exam_case", _set("reproduction.fails_on", "HEAD")),
    # trends
    (Rule.TREND_UNOBSERVED_WHEN_INCOMPARABLE, "exam_case",
     _top("trend", {"exam_scores": "up", "operator_interventions": "unobserved", "notes": ""})),
    (Rule.TREND_UNOBSERVED_WHEN_INCOMPARABLE, "exam_case",
     _top("trend", {"exam_scores": "unobserved", "operator_interventions": "down", "notes": ""})),
]


@pytest.mark.parametrize(
    ("rule", "name", "mutate"),
    CASES,
    ids=[f"{rule.value}-{i}" for i, (rule, _, _) in enumerate(CASES)],
)
def test_breaking_one_rule_rejects_the_file_naming_it(
    rule: Rule, name: str, mutate: Mutation, evidence: StagedEvidence
) -> None:
    doc = example(name)
    mutate(doc)

    assert rule in _rules(doc, evidence)


#: Rules broken through the staged EVIDENCE rather than the findings file;
#: their cases are the tests below.
EVIDENCE_CASE_RULES = {
    Rule.OCCURRENCE_BY_THE_CUTOFF,
    Rule.NOT_NOTICED_NEEDS_COVERAGE,
    Rule.PRESENCE_MATCHES_CURRENT_AUDIT,
    Rule.NOT_NOTICED_UNREFERENCED,
}


def test_every_rule_has_a_case() -> None:
    assert {rule for rule, _, _ in CASES} | EVIDENCE_CASE_RULES == set(Rule)


def test_not_noticed_needs_both_coverage_spans_to_contain_the_window(tmp_path: Path) -> None:
    """Coverage that stops before the cutoff grades unknown, not not_noticed."""
    data = build_improver_data(tmp_path)
    decisions = json.loads((data / "charter-decisions.json").read_text())
    decisions["coverage"]["to"] = "2026-09-28T17:00:00Z"
    (data / "charter-decisions.json").write_text(json.dumps(decisions))
    doc = example("charter_proposal")

    assert Rule.NOT_NOTICED_NEEDS_COVERAGE in _rules(doc, load_staged_evidence(data))


def test_not_noticed_needs_coverage_that_starts_before_the_onset(tmp_path: Path) -> None:
    data = build_improver_data(tmp_path)
    cases = json.loads((data / "case-files.json").read_text())
    cases["coverage"]["from"] = "2026-09-28T13:30:00Z"
    (data / "case-files.json").write_text(json.dumps(cases))

    assert Rule.NOT_NOTICED_NEEDS_COVERAGE in _rules(example("charter_proposal"), load_staged_evidence(data))


def test_not_noticed_without_a_staged_case_file_ledger_is_unknown(tmp_path: Path) -> None:
    data = build_improver_data(tmp_path)
    (data / "case-files.json").unlink()

    assert Rule.NOT_NOTICED_NEEDS_COVERAGE in _rules(example("charter_proposal"), load_staged_evidence(data))


def test_an_onset_inside_a_log_read_that_began_after_it_is_not_proven(tmp_path: Path) -> None:
    """A bounded log whose read starts at the first sighting cannot prove the
    anomaly did not begin earlier (the prompt's round-9 rule)."""
    data = build_improver_data(tmp_path)
    audit = json.loads((data / "audit.json").read_text())
    audit["no_progress"]["log"]["covers_window"] = False
    audit["no_progress"]["log"]["first_entry_at"] = "2026-09-28T13:00:00+00:00"
    (data / "audit.json").write_text(json.dumps(audit))

    assert Rule.NOT_NOTICED_NEEDS_PROVEN_ONSET in _rules(example("charter_proposal"), load_staged_evidence(data))


def test_not_in_charter_without_a_staged_charter_is_unknown(tmp_path: Path) -> None:
    data = build_improver_data(tmp_path)
    (data / "charter.json").unlink()
    doc = example("prompt_proposal")
    _finding(doc)["stall_evidence"] = ["engine-source:src/issue_orchestrator/domain/tech_lead_charter.py"]

    assert Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE in _rules(doc, load_staged_evidence(data))


def test_an_exam_trend_over_the_same_case_set_may_be_reported(tmp_path: Path) -> None:
    evidence = load_staged_evidence(build_improver_data(tmp_path, exam_comparable=True))
    doc = example("exam_case")
    doc["trend"]["exam_scores"] = "up"

    assert validate_findings(json.dumps(doc), evidence).trend.exam_scores == "up"


def test_a_file_that_is_not_json_is_a_schema_rejection_only(evidence: StagedEvidence) -> None:
    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings("not json", evidence)

    assert rejected.value.rules == {Rule.SCHEMA}


def test_a_valid_file_is_returned_typed(evidence: StagedEvidence) -> None:
    findings = validate_findings(json.dumps(example("exam_case")), evidence)

    assert findings.findings[0].grading_window.from_ != "unknown"
    assert findings.findings[0].grading_window.from_ == datetime.fromisoformat(A1_FIRST_SEEN)
    assert findings.findings[0].grading_window.to == CUTOFF


def test_presence_needs_the_anomaly_in_the_current_audit_not_only_in_the_diff(tmp_path: Path) -> None:
    """A key the diff still names (new, resolved, unobserved) is a real key,
    but only the current audit's anomalies show it present now."""
    data = build_improver_data(tmp_path)
    audit = json.loads((data / "audit.json").read_text())
    audit["anomalies"] = [a for a in audit["anomalies"] if a["subject"] != "#353"]
    (data / "audit.json").write_text(json.dumps(audit))

    assert Rule.PRESENCE_MATCHES_CURRENT_AUDIT in _rules(example("needs_investigation"), load_staged_evidence(data))


def test_presence_needs_an_audit_taken_after_the_start(tmp_path: Path) -> None:
    data = build_improver_data(tmp_path)
    start = json.loads((data / "engine-start.json").read_text())
    start["started_at"] = "2026-09-28T18:30:00Z"
    (data / "engine-start.json").write_text(json.dumps(start))
    doc = example("needs_investigation")
    doc["engine_started_at"] = "2026-09-28T18:30:00+00:00"

    assert Rule.PRESENCE_MATCHES_CURRENT_AUDIT in _rules(doc, load_staged_evidence(data))


def _with_notice(data: Path, name: str, mutate: Callable[[dict], None]) -> StagedEvidence:
    document = json.loads((data / name).read_text())
    mutate(document)
    (data / name).write_text(json.dumps(document))
    return load_staged_evidence(data)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        # A decision about #500 inside the grading window (r1 F3).
        ("charter-decisions.json", lambda d: d["decisions"][1].update(target_number=500)),
        # A tech-lead run whose subject is #500.
        ("case-files.json", lambda d: d["diagnoses"][0].update(subject_issue_number=500)),
        # A run that wrote about #500.
        ("case-files.json", lambda d: d["diagnoses"][0].update(body="#500 keeps exploding")),
        # A case file about #500 observed inside the window.
        ("case-files.json", lambda d: d["case_files"][0].update(body="see #500")),
    ],
)
def test_not_noticed_is_refused_when_a_staged_notice_refers_to_the_anomaly(
    tmp_path: Path, name: str, mutate: Callable[[dict], None]
) -> None:
    evidence = _with_notice(build_improver_data(tmp_path), name, mutate)

    assert Rule.NOT_NOTICED_UNREFERENCED in _rules(example("charter_proposal"), evidence)


def test_a_notice_of_another_issue_or_before_the_onset_is_not_a_notice_of_this_one(tmp_path: Path) -> None:
    def elsewhere(d: dict) -> None:
        d["decisions"][1].update(target_number=5000)
        d["decisions"][0].update(target_number=500, decided_at="2026-09-28T12:30:00Z")

    evidence = _with_notice(build_improver_data(tmp_path), "charter-decisions.json", elsewhere)

    assert validate_findings(json.dumps(example("charter_proposal")), evidence).findings


def test_an_occurrence_may_cite_its_whole_record_at_one_of_its_dates(evidence: StagedEvidence) -> None:
    """How a live run cited them: the signature, dated its first_seen or last_seen."""
    doc = example("exam_case")
    for entry in _finding(doc)["observed"][1:]:
        entry["source"] = entry["source"].rsplit("/", 1)[0]

    assert validate_findings(json.dumps(doc), evidence).findings


def test_a_whole_record_citation_still_proves_an_onset(evidence: StagedEvidence) -> None:
    doc = example("charter_proposal")
    _finding(doc)["observed"][1]["source"] = "audit.json#/no_progress/log_signatures/1"

    assert validate_findings(json.dumps(doc), evidence).findings[0].stall_point == "not_noticed"


def test_acted_not_effective_needs_an_applied_decision_about_its_own_issue(tmp_path: Path) -> None:
    """D2 applied, but about another issue: not this anomaly's remedy (r2 F2)."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "charter-decisions.json",
        lambda d: d["decisions"][1].update(target_number=999, anchor_issue_number=999),
    )

    rules = _rules(example("capability_issue"), evidence)

    assert Rule.ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION in rules
    assert Rule.STALL_EVIDENCE_ABOUT_THE_ANOMALY in rules


def test_a_restrictive_charter_setting_is_a_valid_not_in_charter_citation(evidence: StagedEvidence) -> None:
    doc = example("prompt_proposal")
    _finding(doc)["stall_evidence"] = ["charter.json#/roles/general/authority"]

    assert validate_findings(json.dumps(doc), evidence).findings[0].stall_point == "not_in_charter"


def test_an_occurrence_dated_after_the_cutoff_is_outside_the_window(tmp_path: Path) -> None:
    """3a r10: a parked action written between the cutoff and the copy."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "audit.json",
        lambda d: d["action_liveness"]["parked"][0].update(last_failed_at="2026-09-28T18:00:01+00:00"),
    )
    doc = example("capability_issue")
    _finding(doc)["recurs_after_start"] = "true"
    _finding(doc)["origin"] = "unknown"
    _finding(doc)["grading_window"]["from"] = "unknown"
    _finding(doc)["observed"][1] = {
        "at": "2026-09-28T18:00:01+00:00", "kind": "occurrence",
        "source": "audit.json#/action_liveness/parked/0/last_failed_at", "supports": "recurs_after_start",
    }

    assert Rule.OCCURRENCE_BY_THE_CUTOFF in _rules(doc, evidence)
