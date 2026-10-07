"""Design findings are first class, under the same evidence rule (#8001).

A design finding cites what the improver read: a line of a staged file or a
toolbox answer. Every citation is looked up in the run directory; one that
is not there rejects the whole file, as a stall finding's would.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from issue_orchestrator.domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    validate_findings,
)
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from tests.unit.improver_support import AUDITED_REPO, ENGINE_ID, build_improver_data, example

LOG_LINE = "2026-10-04 02:00:01,123 INFO [provider] auth_expired: parking all claude work"
ANSWER = '{"number": 459, "body": "Removed proposed-tech-lead to approve; it came back."}'


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    build_improver_data(run)
    logs = run / "toolbox" / "logs"
    logs.mkdir(parents=True)
    (logs / "orchestrator.log").write_text(
        "".join(f"2026-10-04 01:59:{i:02d},000 INFO tick {i}\n" for i in range(40)) + LOG_LINE + "\n" + "tail\n"
    )
    answers = run / "toolbox-answers"
    answers.mkdir()
    (answers / "3.txt").write_text(ANSWER)
    # The call log the toolbox keeps: call 3 was a GitHub read.
    calls = [
        {"call": 3, "tool": "github_get", "arguments": {"path": "repos/porchpin/porchpin/issues/459"}, "outcome": "ok"},
    ]
    (run / "toolbox-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    (tmp_path / "outside.txt").write_text(LOG_LINE + "\n")
    (run / "agent-workspace").mkdir()
    (run / "agent-workspace" / "planted.txt").write_text(LOG_LINE + "\n")
    return run


def _design(*evidence: dict, id: str = "approval-by-label-removal") -> dict:
    return {
        "id": id,
        "engine": {"id": ENGINE_ID, "repo": AUDITED_REPO},
        "kind": "operator_friction",
        "summary": "Approving a proposal is removing a label: anything that strips labels approves.",
        "evidence": list(evidence),
        "impact": "An unapproved proposal can run; the operator's approval did not stick on #459.",
        "proposed_change": "A positive approval act, recorded with its actor.",
    }


LOG_CITATION = {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": 41, "quote": "auth_expired: parking all claude work"}
TOOL_CITATION = {"kind": "tool", "call": 3, "quote": "Removed proposed-tech-lead to approve"}


def _doc(*designs: dict) -> str:
    doc = example("exam_case")
    doc["design_findings"] = list(designs)
    return json.dumps(doc)


def _validate(run_dir: Path, text: str):  # type: ignore[no-untyped-def]
    return validate_findings(text, load_staged_evidence(run_dir / "improver-data"))


def _rejections(run_dir: Path, text: str) -> list[tuple[str, str | None]]:
    with pytest.raises(ImproverFindingsRejected) as rejected:
        _validate(run_dir, text)
    return [(v.rule.value, v.finding_id) for v in rejected.value.violations]


def test_a_design_finding_whose_every_citation_is_there_is_accepted(run_dir: Path) -> None:
    findings = _validate(run_dir, _doc(_design(LOG_CITATION, TOOL_CITATION)))

    [design] = findings.design_findings
    assert design.kind == "operator_friction" and len(design.evidence) == 2


@pytest.mark.parametrize(
    "citation",
    [
        # Whitespace differs, and the agent counted a line off: still the same text.
        {**LOG_CITATION, "line": 40, "quote": "auth_expired:   parking all\nclaude work"},
        {**LOG_CITATION, "line": 42},
        {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": 1, "quote": "2026-10-04 01:59:00,000 INFO tick 0"},
        # A staged bundle file.
        {"kind": "file", "path": "improver-data/inputs.json", "line": 4, "quote": f"\"audited_repo\": \"{AUDITED_REPO}\""},
    ],
)
def test_a_citation_is_found_through_whitespace_and_a_small_line_slip(run_dir: Path, citation: dict) -> None:
    _validate(run_dir, _doc(_design(citation)))


@pytest.mark.parametrize(
    ("citation", "why"),
    [
        ({**LOG_CITATION, "quote": "auth_expired: paging the operator now"}, "quote_not_found"),
        ({**LOG_CITATION, "line": 30}, "quote_not_found"),
        ({**LOG_CITATION, "line": 999}, "no_such_source"),
        ({**LOG_CITATION, "path": "toolbox/logs/missing.log"}, "no_such_source"),
        ({**TOOL_CITATION, "call": 4}, "no_such_source"),
        ({**TOOL_CITATION, "quote": "Approved by the maintainer"}, "quote_not_found"),
        # Outside the evidence: the run's parent, by traversal.
        ({**LOG_CITATION, "path": "toolbox/../../outside.txt", "line": 1}, "outside_evidence"),
    ],
)
def test_a_citation_that_is_not_there_rejects_the_file(run_dir: Path, citation: dict, why: str) -> None:
    rejections = _rejections(run_dir, _doc(_design(citation)))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


def test_a_symlink_out_of_the_evidence_is_not_evidence(run_dir: Path) -> None:
    os.symlink(run_dir.parent / "outside.txt", run_dir / "toolbox" / "logs" / "linked.log")

    rejections = _rejections(run_dir, _doc(_design({**LOG_CITATION, "path": "toolbox/logs/linked.log", "line": 1})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


@pytest.mark.parametrize(
    "path",
    [
        # The agent's own writable workspace and the prompt are never evidence.
        "agent-workspace/planted.txt",
        "improver-prompt.txt",
        "/etc/hosts",
    ],
)
def test_only_the_staged_evidence_roots_may_be_cited(run_dir: Path, path: str) -> None:
    rejections = _rejections(run_dir, _doc(_design({**LOG_CITATION, "path": path, "line": 1})))

    assert {rule for rule, _ in rejections} == {Rule.SCHEMA.value}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(evidence=[]),
        lambda d: d.update(summary="   "),
        lambda d: d.update(kind="opinion"),
        lambda d: d["evidence"][0].update(quote="too short"),
        lambda d: d["evidence"][0].update(kind="hunch"),
        lambda d: d["evidence"][0].pop("line"),
        lambda d: d.update(extra="field"),
    ],
)
def test_a_design_finding_has_its_shape(run_dir: Path, mutation) -> None:  # type: ignore[no-untyped-def]
    design = _design(dict(LOG_CITATION))
    mutation(design)

    assert {rule for rule, _ in _rejections(run_dir, _doc(design))} == {Rule.SCHEMA.value}


def test_a_design_finding_names_the_staged_engine_and_a_unique_id(run_dir: Path) -> None:
    other_engine = _design(LOG_CITATION)
    other_engine["engine"] = {"id": "repo-" + "b" * 64, "repo": AUDITED_REPO}
    stall_id = example("exam_case")["findings"][0]["id"]

    assert _rejections(run_dir, _doc(other_engine)) == [(Rule.ENGINE_TAG_MATCHES_INPUTS.value, other_engine["id"])]
    assert _rejections(run_dir, _doc(_design(LOG_CITATION, id=stall_id))) == [(Rule.UNIQUE_FINDING_IDS.value, stall_id)]


def test_a_file_without_design_findings_is_refused_at_schema_v5(run_dir: Path) -> None:
    doc = example("exam_case")
    del doc["design_findings"]

    assert {rule for rule, _ in _rejections(run_dir, json.dumps(doc))} == {Rule.SCHEMA.value}


def test_a_quote_padded_with_whitespace_is_still_too_short(run_dir: Path) -> None:
    """r1 F2: the length rule counts what is matched, not the padding."""
    (run_dir / "toolbox" / "logs" / "orchestrator.log").write_text("engine id\n")
    citation = {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": 1, "quote": "engine          id"}

    rejections = _rejections(run_dir, _doc(_design(citation)))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


def _tool_call(run_dir: Path, call: int, tool: str, arguments: dict, answer: str) -> None:
    (run_dir / "toolbox-answers" / f"{call}.txt").write_text(answer)
    with (run_dir / "toolbox-calls.jsonl").open("a") as log:
        log.write(json.dumps({"call": call, "tool": tool, "arguments": arguments, "outcome": "ok"}) + "\n")


def _store(run_dir: Path, value: str) -> None:
    import sqlite3

    state = run_dir / "toolbox" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "timeline.sqlite") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS timeline (event TEXT)")
        conn.execute("INSERT INTO timeline VALUES (?)", (value,))


def test_a_sql_answer_counts_only_for_a_value_the_store_holds(run_dir: Path) -> None:
    """r1 F1: ``SELECT 'any claim'`` answers whatever the agent wrote; only
    a value present in the store copy itself is evidence."""
    stored = "review.skipped: blocked by needs-human on #364"
    _store(run_dir, stored)
    _tool_call(run_dir, 5, "sql_query", {"database": "timeline.sqlite", "sql": "SELECT event FROM timeline"},
               json.dumps({"columns": ["event"], "rows": [[stored]], "truncated": False}))
    planted = "this design defect was observed by the engine"
    _tool_call(run_dir, 6, "sql_query", {"database": "timeline.sqlite", "sql": f"SELECT '{planted}'"},
               json.dumps({"columns": ["x"], "rows": [[planted]], "truncated": False}))

    _validate(run_dir, _doc(_design({"kind": "tool", "call": 5, "quote": stored})))
    rejections = _rejections(run_dir, _doc(_design({"kind": "tool", "call": 6, "quote": planted})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


def test_a_git_answer_is_never_evidence(run_dir: Path) -> None:
    """r1 F1: ``git log --format=<anything>`` writes its own answer."""
    claim = "operator approves by removing a label"
    _tool_call(run_dir, 7, "git", {"args": ["log", "-1", f"--format={claim}"]}, claim + "\n")

    rejections = _rejections(run_dir, _doc(_design({"kind": "tool", "call": 7, "quote": claim})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


def test_an_answer_with_no_logged_call_is_not_evidence(run_dir: Path) -> None:
    (run_dir / "toolbox-answers" / "9.txt").write_text(ANSWER)

    rejections = _rejections(run_dir, _doc(_design({**TOOL_CITATION, "call": 9})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


def test_a_deleted_rows_leftover_bytes_are_not_a_stored_value(run_dir: Path) -> None:
    """r2 F1: with secure_delete off, a deleted row's text stays in a free
    page; a SELECT of that text as a literal must not pass as evidence."""
    import sqlite3

    claim = "operator approval is the removal of a label"
    state = run_dir / "toolbox" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "timeline.sqlite") as conn:
        conn.execute("PRAGMA secure_delete = OFF")
        conn.execute("CREATE TABLE timeline (event TEXT)")
        conn.execute("INSERT INTO timeline VALUES ('review.skipped: kept')")
        conn.execute("INSERT INTO timeline VALUES (?)", (claim,))
        conn.execute("DELETE FROM timeline WHERE event = ?", (claim,))
    assert claim.encode() in (state / "timeline.sqlite").read_bytes()
    _tool_call(run_dir, 8, "sql_query", {"database": "timeline.sqlite", "sql": f"SELECT '{claim}'"},
               json.dumps({"columns": ["x"], "rows": [[claim]], "truncated": False}))

    rejections = _rejections(run_dir, _doc(_design({"kind": "tool", "call": 8, "quote": claim})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]


@pytest.mark.parametrize(
    "planted",
    [
        # r3 F1: the dump's own SQL is not a stored value...
        'INSERT INTO "facts" VALUES(',
        # ...nor is text assembled across two values of a row (a guard: the
        # dump's ',' separator already kept this from matching).
        "kept harmless value','second",
    ],
)
def test_a_sql_quote_must_lie_inside_one_stored_value(run_dir: Path, planted: str) -> None:
    import sqlite3

    state = run_dir / "toolbox" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "facts.sqlite") as conn:
        conn.execute("CREATE TABLE facts (a TEXT, b TEXT)")
        conn.execute("INSERT INTO facts VALUES ('a kept harmless value', 'second value here')")
    _tool_call(run_dir, 11, "sql_query", {"database": "facts.sqlite", "sql": "SELECT 1"},
               json.dumps({"columns": ["x"], "rows": [[planted]], "truncated": False}))
    _tool_call(run_dir, 12, "sql_query", {"database": "facts.sqlite", "sql": "SELECT a FROM facts"},
               json.dumps({"columns": ["a"], "rows": [["a kept harmless value"]], "truncated": False}))

    # Quoted as the answer shows it (JSON-escaped), as an agent would.
    shown = json.dumps(planted)[1:-1]
    assert shown in (run_dir / "toolbox-answers" / "11.txt").read_text()

    rejections = _rejections(run_dir, _doc(_design({"kind": "tool", "call": 11, "quote": shown})))

    assert rejections == [(Rule.DESIGN_CITATION_RESOLVES.value, "approval-by-label-removal")]
    # The stored value itself is still evidence.
    _validate(run_dir, _doc(_design({"kind": "tool", "call": 12, "quote": "a kept harmless value"})))


def test_a_stored_schema_value_is_evidence(run_dir: Path) -> None:
    """r4 F1: ``sqlite_master.sql`` is a live stored value an agent may cite
    (e.g. that a table has no index on the column a query scans)."""
    import sqlite3

    state = run_dir / "toolbox" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "facts.sqlite") as conn:
        conn.execute("CREATE TABLE facts (description TEXT)")
    ddl = "CREATE TABLE facts (description TEXT)"
    _tool_call(run_dir, 13, "sql_query", {"database": "facts.sqlite", "sql": "SELECT sql FROM sqlite_master"},
               json.dumps({"columns": ["sql"], "rows": [[ddl]], "truncated": False}))

    _validate(run_dir, _doc(_design({"kind": "tool", "call": 13, "quote": ddl})))
