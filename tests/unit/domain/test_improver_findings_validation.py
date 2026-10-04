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


def _account(field: str, value: object, index: int = 0) -> Mutation:
    """Set ``blocked_items[index].<field>``."""

    def mutate(doc: Doc) -> None:
        doc["blocked_items"][index][field] = value

    return mutate


def _add_account(account: dict) -> Mutation:
    def mutate(doc: Doc) -> None:
        doc["blocked_items"].append(account)

    return mutate


def _duplicate_account(doc: Doc) -> None:
    doc["blocked_items"].append(json.loads(json.dumps(doc["blocked_items"][0])))


#: #353 accounted for by the exam_case finding, which is about #410.
def _account_to_another_finding(doc: Doc) -> None:
    doc["blocked_items"][0] = {
        "number": 353, "disposition": "finding", "finding_id": _finding(doc)["id"], "why": "w",
    }


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
    # the engine tag (#7567)
    (Rule.SCHEMA, "exam_case", _drop("engine")),
    (Rule.SCHEMA, "exam_case", _top("schema_version", 1)),
    (Rule.ENGINE_TAG_MATCHES_INPUTS, "exam_case", _set("engine.id", "repo-" + "a" * 64)),
    (Rule.ENGINE_TAG_MATCHES_INPUTS, "capability_issue", _set("engine.repo", "issue-orchestrator/issue-orchestrator")),
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
    # "unknown" while the current audit's snapshot settles it (r11 F1).
    (Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "charter_proposal",
     _all(_set("present_after_start", "unknown"), _drop_observed(0))),
    (Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "capability_issue",
     _set("observed.0", {"at": "2026-09-28T10:00:00+00:00", "kind": "occurrence",
                         "source": "audit.json#/action_liveness/parked/0/last_failed_at",
                         "supports": "origin"})),
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "capability_issue", _set("recurs_after_start", "true")),
    # "Does not recur" while the staged records show a post-start occurrence (r1 F2).
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "charter_proposal", _set("recurs_after_start", "false")),
    (Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "exam_case", _set("recurs_after_start", "unknown")),
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
    # A source citation claims a MISSING kind: it must be named, and absent (r12).
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _set("stall_evidence", ["engine-source:src/issue_orchestrator/domain/tech_lead_charter.py"])),
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _all(_set("stall_evidence", ["engine-source:src/issue_orchestrator/domain/tech_lead_charter.py"]),
          _drop("remedy_action_kind"), _set("missing_action_kind", "reset_retry"))),
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "exam_case", _set("missing_action_kind", "clear_refusal")),
    # A restrictive setting of ANOTHER role does not hold this remedy back (r14 F2).
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _all(_set("remedy_action_kind", "post_comment"),
          _set("stall_evidence", ["charter.json#/roles/general/authority"]))),
    # Both kinds named at once.
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal", _set("missing_action_kind", "clear_it")),
    # An existing kind claimed missing beside a restrictive citation (r13).
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal", _set("missing_action_kind", "reset_retry")),
    # A missing kind with no source citation.
    (Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "prompt_proposal",
     _all(_set("stall_evidence", ["charter.json#/actions/reset_retry/outcome"]),
          _drop("remedy_action_kind"), _set("missing_action_kind", "clear_validation_refusal"))),
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
    # blocked items: every one accounted for, each account backed
    (Rule.BLOCKED_ITEMS_ACCOUNTED, "exam_case", _top("blocked_items", [])),
    (Rule.BLOCKED_ITEMS_ACCOUNTED, "needs_investigation", _top("blocked_items", [])),
    (Rule.BLOCKED_ITEMS_ACCOUNTED, "exam_case",
     _add_account({"number": 999, "disposition": "awaiting_operator", "evidence": ["D3"], "why": "w"})),
    (Rule.BLOCKED_ITEMS_ACCOUNTED, "exam_case", _duplicate_account),
    (Rule.BLOCKED_ITEM_ACCOUNT_SHAPE, "exam_case", _account("finding_id", "parked-publish-divergent-heads")),
    (Rule.BLOCKED_ITEM_ACCOUNT_SHAPE, "needs_investigation", _account("evidence", ["D3"])),
    (Rule.BLOCKED_ITEM_ACCOUNT_SHAPE, "needs_investigation", _account("finding_id", "no-such-finding")),
    (Rule.BLOCKED_ITEM_FINDING_ABOUT_IT, "exam_case", _account_to_another_finding),
    # Still blocked after the start: live, whatever its origin (handover #1).
    (Rule.BLOCKED_ITEM_FINDING_ABOUT_IT, "needs_investigation",
     _all(_set("present_after_start", "false"), _set("recurs_after_start", "true"))),
    # Someone else's remedy is not this item handed over.
    (Rule.BLOCKED_ITEM_HANDED_OVER, "exam_case", _account("evidence", ["D2"])),
    # A diagnosis is not a hand-over: a flag (D1) is not an escalation.
    (Rule.BLOCKED_ITEM_HANDED_OVER, "exam_case", _account("evidence", [])),
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
    Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED,
    Rule.NOTICED_NOT_ACTED_WITHOUT_AN_APPLIED_REMEDY,
    Rule.OCCURRENCE_BY_THE_CUTOFF,
    Rule.NOT_NOTICED_NEEDS_COVERAGE,
    Rule.PRESENCE_MATCHES_CURRENT_AUDIT,
    Rule.NOT_NOTICED_UNREFERENCED,
}


def test_a_finding_about_one_engine_is_rejected_against_another_engines_inputs(tmp_path: Path) -> None:
    """Two engines are audited per sweep (#7567): a finding valid for porchpin's
    staged inputs is refused when validated against another engine's."""
    data = build_improver_data(tmp_path)
    manifest = json.loads((data / "inputs.json").read_text())
    manifest["engine_id"] = "repo-" + "b" * 64
    (data / "inputs.json").write_text(json.dumps(manifest))

    assert _rules(example("exam_case"), load_staged_evidence(data)) == {Rule.ENGINE_TAG_MATCHES_INPUTS}


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
    _finding(doc)["missing_action_kind"] = "clear_validation_refusal"

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


def test_a_setting_that_holds_the_remedy_back_is_a_valid_not_in_charter_citation(evidence: StagedEvidence) -> None:
    doc = example("prompt_proposal")
    _finding(doc)["remedy_action_kind"] = "promote_finding"
    _finding(doc)["stall_evidence"] = ["charter.json#/actions/promote_finding/action_ceiling"]

    assert validate_findings(json.dumps(doc), evidence).findings[0].remedy_action_kind == "promote_finding"


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


def test_noticed_not_acted_is_refused_when_a_remedy_about_it_was_applied(tmp_path: Path) -> None:
    """3a r11 F2: an applied remedy about #410 means it was acted on."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "charter-decisions.json",
        lambda d: d["decisions"][0].update(binding="approvable", applied_at="2026-09-28T14:00:00Z"),
    )

    assert Rule.NOTICED_NOT_ACTED_WITHOUT_AN_APPLIED_REMEDY in _rules(example("exam_case"), evidence)


def test_a_missing_action_kind_with_its_source_is_a_valid_not_in_charter_claim(evidence: StagedEvidence) -> None:
    doc = example("prompt_proposal")
    _finding(doc)["stall_evidence"] = ["engine-source:src/issue_orchestrator/domain/tech_lead_charter.py"]
    _finding(doc)["missing_action_kind"] = "clear_validation_refusal"
    del _finding(doc)["remedy_action_kind"]

    assert validate_findings(json.dumps(doc), evidence).findings[0].missing_action_kind == "clear_validation_refusal"


# -- blocked items: the operator's objective (handover grade #1) --------------


def test_a_hand_over_applied_before_the_item_was_blocked_does_not_account_for_it(tmp_path: Path) -> None:
    """An escalation of an EARLIER block is not the tech lead acting on this one."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json",
        lambda d: d["items"][0]["decisions"][0].update(applied_at="2026-09-28T13:00:00Z"),
    )

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def test_an_item_blocked_since_an_unknown_time_cannot_be_shown_handed_over(tmp_path: Path) -> None:
    def unknown_since(d: dict) -> None:
        d["items"][0]["blocked_since"] = None
        d["items"][0]["blocking_labels"][0]["since_at"] = None

    evidence = _with_notice(build_improver_data(tmp_path), "blocked-items.json", unknown_since)

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def test_a_remedy_is_not_a_hand_over(tmp_path: Path) -> None:
    """Only an escalation, a deferral or an explaining comment hands an item
    to the operator; an applied remedy that left it blocked is a finding."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json",
        lambda d: d["items"][0]["decisions"][0].update(action_kind="reset_retry", binding="destructive"),
    )

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def test_without_staged_blocked_items_nothing_is_accounted_for(tmp_path: Path) -> None:
    data = build_improver_data(tmp_path)
    (data / "blocked-items.json").unlink()
    evidence = load_staged_evidence(data)
    doc = example("exam_case")

    assert _rules(doc, evidence) == {Rule.BLOCKED_ITEMS_ACCOUNTED}
    doc["blocked_items"] = []
    assert validate_findings(json.dumps(doc), evidence).blocked_items == ()


def test_a_blocked_items_decision_from_before_the_window_is_a_notice(tmp_path: Path) -> None:
    """blocked-items.json carries the WHOLE ledger's decisions about an item:
    one older than the observation window still resolves as stall evidence."""
    def older(d: dict) -> None:
        d["items"][0]["decisions"].append(
            {**d["items"][0]["decisions"][0], "decision_id": "D0", "action_kind": "post_comment",
             "binding": "advisory", "decided_at": "2026-09-20T10:00:00Z", "applied_at": "2026-09-20T10:00:00Z"}
        )

    evidence = _with_notice(build_improver_data(tmp_path), "blocked-items.json", older)
    doc = example("needs_investigation")
    _finding(doc)["stall_point"] = "noticed_not_acted"
    _finding(doc)["stall_evidence"] = ["D0"]

    assert validate_findings(json.dumps(doc), evidence).findings[0].stall_evidence == ("D0",)


def test_a_remedy_applied_before_the_onset_does_not_make_a_later_notice_acted_on(tmp_path: Path) -> None:
    """noticed_not_acted is refused only by a remedy applied inside the
    grading window: one applied before the anomaly began acted on something else."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "charter-decisions.json",
        lambda d: d["decisions"][0].update(binding="approvable", applied_at="2026-09-27T08:00:00Z"),
    )

    assert validate_findings(json.dumps(example("exam_case")), evidence).findings[0].stall_point == (
        "noticed_not_acted"
    )


def test_a_comment_is_not_a_hand_over(tmp_path: Path) -> None:
    """r1 F2: a staged comment does not say what it said, so it cannot show a hand-over."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json",
        lambda d: d["items"][0]["decisions"][0].update(action_kind="post_comment", binding="advisory"),
    )

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


@pytest.mark.parametrize(
    "change",
    [
        # Advice applied (a comment, a flag) is not a remedy (r1 F3).
        {"binding": "advisory"},
        # A remedy applied before the anomaly's onset acted on an earlier occurrence.
        {"applied_at": "2026-09-28T09:00:00Z"},
    ],
)
def test_acted_not_effective_needs_a_remedy_applied_inside_the_window(tmp_path: Path, change: dict) -> None:
    evidence = _with_notice(
        build_improver_data(tmp_path), "charter-decisions.json", lambda d: d["decisions"][1].update(**change),
    )

    assert Rule.ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION in _rules(example("capability_issue"), evidence)


def test_a_finding_about_another_anomaly_of_the_issue_does_not_account_for_its_block(tmp_path: Path) -> None:
    """r2 F3: #353 also has an owed pause; a finding keyed only to that leaves its block unexamined."""
    def owed_pause(d: dict) -> None:
        d["anomalies"].append({
            "kind": "owed_pause", "sources": ["action_liveness"], "subject": "#353",
            "signature": "reconcile_pause", "detail": "owed", "count": 1,
        })

    evidence = _with_notice(build_improver_data(tmp_path), "audit.json", owed_pause)
    doc = example("needs_investigation")
    _finding(doc)["anomaly_keys"] = [{"kind": "owed_pause", "subject": "#353", "signature": "reconcile_pause"}]

    assert Rule.BLOCKED_ITEM_FINDING_ABOUT_IT in _rules(doc, evidence)


def _second_block(at: str) -> Callable[[dict], None]:
    """#353 also carries recovery-pending, put on at ``at``."""
    def mutate(d: dict) -> None:
        d["items"][0]["labels"].append("recovery-pending")
        d["items"][0]["blocking_labels"].append(
            {"label": "recovery-pending", "since_at": at, "since_event": "issue.labels_changed"}
        )

    return mutate


def test_a_hand_over_before_a_later_block_does_not_account_for_it(tmp_path: Path) -> None:
    """r3 F2: D3 escalated #353's needs-human (13:45); recovery-pending went on
    later (14:30). The newer block was never handed over."""
    evidence = _with_notice(build_improver_data(tmp_path), "blocked-items.json", _second_block("2026-09-28T14:30:00Z"))

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def test_a_hand_over_after_every_current_block_accounts_for_it(tmp_path: Path) -> None:
    evidence = _with_notice(build_improver_data(tmp_path), "blocked-items.json", _second_block("2026-09-28T13:35:00Z"))

    assert validate_findings(json.dumps(example("exam_case")), evidence).blocked_items[0].disposition == (
        "awaiting_operator"
    )


def test_an_item_whose_block_may_have_been_put_back_cannot_be_handed_over(tmp_path: Path) -> None:
    """r7 F1: with the current block's onset unknown (an uncertain re-add),
    an earlier escalation cannot be shown to cover it."""
    def unknown_since(d: dict) -> None:
        d["items"][0]["blocked_since"] = None
        d["items"][0]["blocking_labels"][0].update(since_at=None, since_event=None)

    evidence = _with_notice(build_improver_data(tmp_path), "blocked-items.json", unknown_since)

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def test_a_deferral_to_a_tracker_is_not_a_hand_over_of_the_whole_item(tmp_path: Path) -> None:
    """r8 F2: a defer_to_tracker hands over one failure, not an agent's question beside it."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json",
        lambda d: d["items"][0]["decisions"][0].update(action_kind="defer_to_tracker"),
    )

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def _decision_put_to_the_operator(*, filed: bool) -> Callable[[dict], None]:
    """#353's D3 is a propose_decision instead: the operator's call (#7593)."""
    def mutate(d: dict) -> None:
        d["items"][0]["decisions"][0].update(
            action_kind="propose_decision", binding="operator_decision", outcome="proposed",
            reason_code="operator_decision_always_proposed", effect="awaiting_approval",
            applied_at=None, proposal_issue_number=950 if filed else None,
        )

    return mutate


def test_a_filed_decision_awaiting_the_operator_hands_the_item_over(tmp_path: Path) -> None:
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json", _decision_put_to_the_operator(filed=True)
    )

    assert validate_findings(json.dumps(example("exam_case")), evidence).blocked_items[0].disposition == (
        "awaiting_operator"
    )


def test_a_decision_whose_proposal_never_got_filed_hands_nothing_over(tmp_path: Path) -> None:
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json", _decision_put_to_the_operator(filed=False)
    )

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rules(example("exam_case"), evidence)


def _two_blocks_with_a_remedy_between(tmp_path: Path) -> StagedEvidence:
    """#353: needs-human since 13:30, recovery-pending since 14:30 (both in the
    audit), and a remedy R1 about #353 applied at 14:00, between them."""
    data = build_improver_data(tmp_path)
    _with_notice(data, "audit.json", lambda d: d["anomalies"].append({
        "kind": "attention_label", "sources": ["github"], "subject": "#353",
        "signature": "recovery-pending", "detail": "open issue carries recovery-pending", "count": None,
    }))

    def blocked(d: dict) -> None:
        _second_block("2026-09-28T14:30:00Z")(d)
        d["items"][0]["decisions"].append({
            **d["items"][0]["decisions"][0], "decision_id": "R1", "action_kind": "reset_retry",
            "binding": "destructive", "decided_at": "2026-09-28T13:55:00Z", "applied_at": "2026-09-28T14:00:00Z",
        })

    return _with_notice(data, "blocked-items.json", blocked)


def test_a_finding_must_key_every_current_block_of_the_item(tmp_path: Path) -> None:
    """r9 F1: keyed to needs-human only, it leaves recovery-pending unexamined."""
    evidence = _two_blocks_with_a_remedy_between(tmp_path)

    assert Rule.BLOCKED_ITEM_FINDING_ABOUT_IT in _rules(example("needs_investigation"), evidence)


def test_a_remedy_before_the_latest_block_did_not_act_on_it(tmp_path: Path) -> None:
    """r9 F2: R1 (14:00) predates the recovery-pending block (14:30): a notice
    without a later remedy is noticed_not_acted, and R1 cannot make it acted on."""
    evidence = _two_blocks_with_a_remedy_between(tmp_path)
    doc = example("needs_investigation")
    _finding(doc)["anomaly_keys"].append({"kind": "attention_label", "subject": "#353", "signature": "recovery-pending"})
    _finding(doc)["observed"].append({
        "at": "2026-09-28T18:00:00+00:00", "kind": "snapshot", "source": "audit.json#/anomalies/4",
        "supports": "present_after_start",
    })
    _finding(doc)["stall_point"] = "noticed_not_acted"
    _finding(doc)["stall_evidence"] = ["D3"]

    assert validate_findings(json.dumps(doc), evidence).findings[0].stall_point == "noticed_not_acted"
    _finding(doc)["stall_point"] = "acted_not_effective"
    _finding(doc)["stall_evidence"] = ["R1"]
    assert Rule.ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION in _rules(doc, evidence)


_REFUSED = {
    "kind": "refused_work", "sources": ["log", "timeline"], "subject": "PR #379", "signature": "review:issue_blocked",
    "detail": "9 refusal(s) since it last changed state", "count": 9,
}


def _refused_review_of_the_items_pr(tmp_path: Path) -> tuple[StagedEvidence, int]:
    """#353's open PR #379: its review found and dropped on every scan because
    #353 is blocked (porchpin #364/#379, 2026-10-02). The audit names it; the
    item carries it as its stalled work. Returns the anomaly's index."""
    data = build_improver_data(tmp_path)
    index: list[int] = []

    def audit(d: dict) -> None:
        d["no_progress"]["refused_work"].append({
            "subject": "PR #379", "related": ["#353"], "action": "review", "reason": "issue_blocked",
            "loggers": ["issue_orchestrator.control.pr_scanner"],
            "example": "[SCANNER] Skipping stale review PR: pr=N issue=N reason=issue_blocked",
            "count": 9, "since_state_change": 9,
            "first_seen": "2026-09-28T14:00:00+00:00", "last_seen": "2026-09-28T17:50:00+00:00",
        })
        index.append(len(d["anomalies"]))
        d["anomalies"].append(_REFUSED)

    _with_notice(data, "audit.json", audit)
    evidence = _with_notice(data, "blocked-items.json", lambda d: d["items"][0]["stalled_work"].append(
        {key: _REFUSED[key] for key in ("kind", "subject", "signature", "detail")}
    ))
    return evidence, index[0]


def test_work_a_block_keeps_refusing_needs_a_finding_that_keys_it(tmp_path: Path) -> None:
    """The blind run graded #364's block and never saw its PR's review dropped
    on every scan: grading the block does not examine what it holds up."""
    evidence, _ = _refused_review_of_the_items_pr(tmp_path)

    assert _rules(example("needs_investigation"), evidence) == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


def test_a_hand_over_of_the_block_does_not_examine_the_work_it_refuses(tmp_path: Path) -> None:
    evidence, _ = _refused_review_of_the_items_pr(tmp_path)

    assert _rules(example("exam_case"), evidence) == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


def test_a_key_without_its_own_evidence_does_not_examine_the_refused_work(tmp_path: Path) -> None:
    """Review r1 F3: the refusal's key slipped into a finding about something
    else, with nothing of it cited, examines nothing."""
    evidence, _ = _refused_review_of_the_items_pr(tmp_path)
    doc = example("needs_investigation")
    _finding(doc)["anomaly_keys"].append({key: _REFUSED[key] for key in ("kind", "subject", "signature")})
    _finding(doc)["observed"].append({
        "at": "2026-09-28T14:00:00+00:00", "kind": "occurrence",
        "source": "audit.json#/no_progress/refused_work/0/first_seen", "supports": "recurs_after_start",
    })
    doc["blocked_items"][0]["downstream"] = [_DOWNSTREAM]

    rejected = _rules(doc, evidence)

    assert rejected == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


_DOWNSTREAM = {
    "anomaly_key": {key: _REFUSED[key] for key in ("kind", "subject", "signature")},
    "finding_id": "needs-human-353",
    "refused_action": "review",
    "impact": "PR #379's published work can never be reviewed while #353 is blocked",
}


def _examined(doc: Doc, index: int) -> Doc:
    """``doc`` with its finding keyed to the refusal, citing its records."""
    _finding(doc)["anomaly_keys"].append({key: _REFUSED[key] for key in ("kind", "subject", "signature")})
    _finding(doc)["observed"].append({
        "at": "2026-09-28T14:00:00+00:00", "kind": "occurrence",
        "source": "audit.json#/no_progress/refused_work/0/first_seen", "supports": "recurs_after_start",
    })
    _finding(doc)["observed"].append({
        "at": "2026-09-28T18:00:00+00:00", "kind": "snapshot", "source": f"audit.json#/anomalies/{index}",
        "supports": "present_after_start",
    })
    return doc


def test_a_keyed_refusal_the_items_account_leaves_out_is_not_accounted_for(tmp_path: Path) -> None:
    """Review r4: graded in a finding, the refusal still needs its item's
    downstream account, naming the impact."""
    evidence, index = _refused_review_of_the_items_pr(tmp_path)

    assert _rules(_examined(example("needs_investigation"), index), evidence) == {
        Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED
    }


def test_downstream_names_only_the_items_stalled_work(tmp_path: Path) -> None:
    doc = example("needs_investigation")
    doc["blocked_items"][0]["downstream"] = [_DOWNSTREAM]

    assert _rules(doc, load_staged_evidence(build_improver_data(tmp_path))) == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


def test_a_hand_over_accounts_for_its_items_downstream_through_a_finding(tmp_path: Path) -> None:
    """An awaiting_operator item: the refusal is still graded, by a finding."""
    evidence, index = _refused_review_of_the_items_pr(tmp_path)
    doc = example("exam_case")
    finding = _examined(example("needs_investigation"), index)["findings"][0]
    doc["findings"].append(finding)
    doc["blocked_items"][0]["downstream"] = [_DOWNSTREAM]

    assert validate_findings(json.dumps(doc), evidence).blocked_items[0].downstream[0].finding_id == (
        "needs-human-353"
    )


def test_a_finding_keyed_to_the_refused_work_examines_it(tmp_path: Path) -> None:
    """Keyed, the refusal's dated records are its occurrences: its
    ``first_seen`` is citable, like a log signature's."""
    evidence, index = _refused_review_of_the_items_pr(tmp_path)
    doc = example("needs_investigation")
    doc["blocked_items"][0]["downstream"] = [_DOWNSTREAM]
    _finding(doc)["anomaly_keys"].append({key: _REFUSED[key] for key in ("kind", "subject", "signature")})
    _finding(doc)["observed"].append({
        "at": "2026-09-28T14:00:00+00:00", "kind": "occurrence",
        "source": "audit.json#/no_progress/refused_work/0/first_seen", "supports": "recurs_after_start",
    })
    _finding(doc)["observed"].append({
        "at": "2026-09-28T18:00:00+00:00", "kind": "snapshot", "source": f"audit.json#/anomalies/{index}",
        "supports": "present_after_start",
    })

    assert validate_findings(json.dumps(doc), evidence).findings[0].anomaly_keys[-1].subject == "PR #379"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # The refused action is the one the anomaly names (review).
        ({"refused_action": "rework"}, Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED),
        ({"refused_action": None}, Rule.SCHEMA),
        # The impact is stated, not blank.
        ({"impact": " "}, Rule.SCHEMA),
    ],
)
def test_a_downstream_claim_is_typed_and_stated(tmp_path: Path, change: dict, rule: Rule) -> None:
    """Review r5: a downstream entry claims WHAT is refused, in so many words."""
    evidence, index = _refused_review_of_the_items_pr(tmp_path)
    doc = _examined(example("needs_investigation"), index)
    doc["blocked_items"][0]["downstream"] = [{**_DOWNSTREAM, **change}]

    assert rule in _rules(doc, evidence)


# -- resolve_block (#7658) ------------------------------------------------------


def _resolution(effect: str, *, proposal: int | None, applied_at: str | None) -> Callable[[dict], None]:
    """#353's D3 is a resolve_block instead: the tech lead decided the block."""
    def mutate(d: dict) -> None:
        d["items"][0]["decisions"][0].update(
            action_kind="resolve_block", binding="approvable",
            outcome="proposed" if effect == "awaiting_approval" else "executed",
            reason_code="action_authority_propose" if effect == "awaiting_approval" else "within_charter_execute",
            effect=effect, applied_at=applied_at, proposal_issue_number=proposal,
        )

    return mutate


def test_a_filed_resolution_awaiting_approval_hands_the_item_to_the_operator(tmp_path: Path) -> None:
    evidence = _with_notice(
        build_improver_data(tmp_path), "blocked-items.json",
        _resolution("awaiting_approval", proposal=951, applied_at=None),
    )

    assert validate_findings(json.dumps(example("exam_case")), evidence).blocked_items[0].disposition == (
        "awaiting_operator"
    )


def test_an_applied_resolution_is_the_tech_lead_acting_on_the_item(tmp_path: Path) -> None:
    """An applied resolve_block about the item refuses a noticed_not_acted grade."""
    evidence = _with_notice(
        build_improver_data(tmp_path), "charter-decisions.json",
        lambda d: d["decisions"][0].update(
            action_kind="resolve_block", binding="approvable", applied_at="2026-09-28T14:00:00Z",
        ),
    )

    assert Rule.NOTICED_NOT_ACTED_WITHOUT_AN_APPLIED_REMEDY in _rules(example("exam_case"), evidence)
