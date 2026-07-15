"""Unit tests for generic E2E result report parsing."""

import os
from pathlib import Path

import pytest

from issue_orchestrator.infra.e2e_reports import (
    CONFIGURED_JUNIT_XML_PATHS_NO_FRESH_FILES_ERROR,
    E2EArtifactCollectionState,
    E2ERunArtifactRecord,
    MAX_CAPTURED_OUTPUT_CHARS,
    JUnitCaseResult,
    classify_e2e_artifact_collection,
    discover_report_artifacts,
    normalize_pytest_junit_cases,
    parse_junit_report,
    parse_junit_report_cached,
    snapshot_report_artifacts,
)


def test_parse_junit_report_extracts_cases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_passes" time="1.25" />
  <testcase classname="pkg.test_mod" name="test_fails" time="2.50">
    <failure message="AssertionError">expected 1, got 2</failure>
  </testcase>
  <testcase classname="pkg.test_mod" name="test_skipped">
    <skipped message="not enabled" />
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert [case.case_id for case in cases] == [
        "pkg.test_mod::test_passes",
        "pkg.test_mod::test_fails",
        "pkg.test_mod::test_skipped",
    ]
    assert cases[0].outcome == "passed"
    assert cases[0].duration_seconds == 1.25
    assert cases[1].outcome == "failed"
    assert cases[1].failure_summary == "AssertionError"
    assert "expected 1, got 2" in (cases[1].failure_details or "")
    assert cases[2].outcome == "skipped"


def test_parse_junit_report_preserves_error_outcome_under_testsuites_root(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuites>
  <testsuite name="suite">
    <testcase classname="pkg.test_mod" name="test_errors" time="0.50">
      <error message="RuntimeError">kaboom</error>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert len(cases) == 1
    assert cases[0].outcome == "error"
    assert cases[0].failure_summary == "RuntimeError"
    assert cases[0].failure_details == "RuntimeError\nkaboom"


def test_parse_junit_report_rejects_missing_testcases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite />", encoding="utf-8")

    with pytest.raises(ValueError, match="did not contain any <testcase>"):
        parse_junit_report(report)


def test_parse_junit_report_rejects_missing_case_name(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite><testcase classname='pkg.test_mod' /></testsuite>", encoding="utf-8")

    with pytest.raises(ValueError, match="missing a non-empty name"):
        parse_junit_report(report)


def test_parse_junit_report_rejects_invalid_time_values(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite>
  <testcase classname="pkg.test_mod" name="test_bad_time" time="not-a-float" />
</testsuite>
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="could not convert string to float"):
        parse_junit_report(report)


def test_parse_junit_report_reports_parse_location(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite>\n  <testcase></testsuite>", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Malformed JUnit XML: .*line 2, column 14"):
        parse_junit_report(report)


def test_normalize_pytest_junit_cases_matches_runtime_nodeids() -> None:
    normalized = normalize_pytest_junit_cases(
        [
            JUnitCaseResult(
                case_id="tests.e2e.test_basic::test_passing",
                display_name="test_passing",
                suite_name="tests.e2e.test_basic",
                outcome="passed",
                duration_seconds=0.12,
            ),
            JUnitCaseResult(
                case_id="tests.e2e.test_claim_coordination.TestClaimCoordination::test_claim",
                display_name="test_claim",
                suite_name="tests.e2e.test_claim_coordination.TestClaimCoordination",
                outcome="failed",
                duration_seconds=0.56,
                failure_details="claim failed",
            ),
            JUnitCaseResult(
                case_id="tests/e2e/test_existing.py::test_already_normalized",
                display_name="test_already_normalized",
                suite_name="tests/e2e/test_existing.py",
                outcome="failed",
                duration_seconds=0.34,
                failure_details="boom",
            ),
        ]
    )

    assert normalized[0].suite_name == "tests/e2e/test_basic.py"
    assert normalized[0].case_id == "tests/e2e/test_basic.py::test_passing"
    assert normalized[1].suite_name == "tests/e2e/test_claim_coordination.py"
    assert normalized[1].case_id == (
        "tests/e2e/test_claim_coordination.py::TestClaimCoordination::test_claim"
    )
    assert normalized[2].suite_name == "tests/e2e/test_existing.py"
    assert normalized[2].case_id == "tests/e2e/test_existing.py::test_already_normalized"


def test_snapshot_report_artifacts_copies_into_run_scoped_directory(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    report = source_dir / "results.xml"
    report.write_text("<testsuite />", encoding="utf-8")
    log = source_dir / "results.log"
    log.write_text("output", encoding="utf-8")
    destination = tmp_path / ".issue-orchestrator" / "e2e-results" / "run_12"

    copied = snapshot_report_artifacts(
        [
            E2ERunArtifactRecord("junit_xml", "JUnit XML: results.xml", str(report)),
            E2ERunArtifactRecord("raw_log", "Raw Log: results.log", str(log)),
        ],
        destination,
    )

    assert [Path(artifact.path).parent for artifact in copied] == [
        destination,
        destination,
    ]
    assert (destination / "results.xml").read_text(encoding="utf-8") == "<testsuite />"
    assert (destination / "results.log").read_text(encoding="utf-8") == "output"


def test_snapshot_report_artifacts_dedupes_same_basename(tmp_path: Path) -> None:
    left = tmp_path / "left" / "results.xml"
    right = tmp_path / "right" / "results.xml"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")

    copied = snapshot_report_artifacts(
        [
            E2ERunArtifactRecord("junit_xml", "left", str(left)),
            E2ERunArtifactRecord("junit_xml", "right", str(right)),
        ],
        tmp_path / "run_1",
    )

    assert [Path(artifact.path).name for artifact in copied] == [
        "results.xml",
        "results-2.xml",
    ]


def test_discover_report_artifacts_classifies_and_dedupes_files(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    junit_report = reports_dir / "results.xml"
    junit_report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="ui.smoke" name="test_homepage" time="1.0" />
</testsuite>
""",
        encoding="utf-8",
    )
    html_report = reports_dir / "report.html"
    html_report.write_text("<html><body>ok</body></html>", encoding="utf-8")
    summary = reports_dir / "summary.txt"
    summary.write_text("status=passed\n", encoding="utf-8")
    trace = reports_dir / "trace.zip"
    trace.write_bytes(b"PK\x03\x04")

    cases, artifacts = discover_report_artifacts(
        tmp_path,
        junit_xml_paths=["reports/results.xml"],
        artifact_paths=[
            "reports/report.html",
            "reports/*.txt",
            "reports/trace.zip",
            "reports/report.html",
        ],
    )

    assert [case.case_id for case in cases] == ["ui.smoke::test_homepage"]
    assert [(artifact.kind, artifact.label, Path(artifact.path).name) for artifact in artifacts] == [
        ("junit_xml", "JUnit XML: results.xml", "results.xml"),
        ("html_report", "HTML Report: report.html", "report.html"),
        ("text_artifact", "Text Artifact: summary.txt", "summary.txt"),
        ("trace", "Trace: trace.zip", "trace.zip"),
    ]


def test_discover_report_artifacts_rejects_missing_configured_artifact_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Configured artifact paths did not resolve"):
        discover_report_artifacts(
            tmp_path,
            junit_xml_paths=[],
            artifact_paths=["reports/missing.log"],
        )


def test_discover_report_artifacts_rejects_stale_configured_junit(
    tmp_path: Path,
) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    junit_report = reports_dir / "results.xml"
    junit_report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="ui.smoke" name="test_homepage" time="1.0" />
</testsuite>
""",
        encoding="utf-8",
    )
    stale_ns = 1_700_000_000_000_000_000
    os.utime(junit_report, ns=(stale_ns, stale_ns))

    with pytest.raises(
        ValueError,
        match=CONFIGURED_JUNIT_XML_PATHS_NO_FRESH_FILES_ERROR,
    ):
        discover_report_artifacts(
            tmp_path,
            junit_xml_paths=["reports/results.xml"],
            artifact_paths=[],
            modified_after=(stale_ns / 1_000_000_000) + 1,
        )


def test_parse_junit_report_extracts_system_out_and_system_err(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_chatty" time="0.10">
    <system-out>captured stdout line 1
captured stdout line 2</system-out>
    <system-err>warning: something happened</system-err>
  </testcase>
  <testcase classname="pkg.test_mod" name="test_quiet" time="0.05" />
  <testcase classname="pkg.test_mod" name="test_blank_channels" time="0.05">
    <system-out>   </system-out>
    <system-err></system-err>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].system_out == "captured stdout line 1\ncaptured stdout line 2"
    assert cases[0].system_err == "warning: something happened"
    assert cases[1].system_out is None
    assert cases[1].system_err is None
    assert cases[2].system_out is None
    assert cases[2].system_err is None


def test_parse_junit_report_caps_captured_output_at_limit(tmp_path: Path) -> None:
    huge = "x" * (MAX_CAPTURED_OUTPUT_CHARS + 5_000)
    report = tmp_path / "junit.xml"
    report.write_text(
        f"""\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_loud" time="0.10">
    <system-out>{huge}</system-out>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].system_out is not None
    assert len(cases[0].system_out) == MAX_CAPTURED_OUTPUT_CHARS


def test_parse_junit_report_attaches_captured_output_to_failed_case(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_fails" time="2.50">
    <failure message="AssertionError">expected 1, got 2</failure>
    <system-out>print before failure</system-out>
    <system-err>traceback noise</system-err>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].outcome == "failed"
    assert cases[0].failure_summary == "AssertionError"
    assert cases[0].system_out == "print before failure"
    assert cases[0].system_err == "traceback noise"


def test_parse_junit_report_cached_reuses_parse_for_unchanged_file(tmp_path: Path) -> None:
    # tmp_path is unique per test, so the cache key is unique and we don't
    # need to reach into the LRU's internals to reset shared state.
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_a" time="0.10">
    <system-out>captured a</system-out>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    raw_a, norm_a = parse_junit_report_cached(report)
    raw_b, norm_b = parse_junit_report_cached(report)

    # Object identity is the observable contract: a cache hit returns the
    # SAME tuple, never a fresh parse. parse_junit_report always returns
    # fresh objects, so reuse-by-identity here is only possible if the cache
    # short-circuited the second call.
    assert raw_a is raw_b
    assert norm_a is norm_b
    # Sanity-check the parsed content is what we expect, not stale or empty.
    assert raw_a[0].system_out == "captured a"


def test_parse_junit_report_cached_invalidates_when_file_is_rewritten(
    tmp_path: Path,
) -> None:
    import os

    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_a" time="0.10">
    <system-out>first</system-out>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    # Pin mtime deterministically — sleeping to wait for the FS clock to tick
    # would flake on filesystems with coarse mtime resolution (HFS+, FAT) or
    # slow CI clocks. The cache key is (path, mtime_ns, size); both must
    # change to force a miss, so we also vary the body length below.
    original_ns = 1_700_000_000_000_000_000
    os.utime(report, ns=(original_ns, original_ns))

    raw_first, _ = parse_junit_report_cached(report)
    assert raw_first[0].system_out == "first"

    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_a" time="0.10">
    <system-out>second-with-different-length</system-out>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    rewritten_ns = original_ns + 1_000_000_000  # one second later
    os.utime(report, ns=(rewritten_ns, rewritten_ns))

    raw_second, _ = parse_junit_report_cached(report)
    # If the cache had ignored the file change, this would still say "first".
    # Observing the new content proves invalidation without inspecting LRU state.
    assert raw_second[0].system_out == "second-with-different-length"
    # Identity also flips — fresh tuple, not the cached one.
    assert raw_second is not raw_first


def test_discover_report_artifacts_rejects_paths_outside_repo_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-report.log"
    outside.write_text("hello\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside repo root"):
        discover_report_artifacts(
            tmp_path,
            junit_xml_paths=[],
            artifact_paths=[f"../{outside.name}"],
        )


class TestClassifyE2EArtifactCollection:
    """Read-side classification of a run's artifact-collection outcome (#6593)."""

    def test_collected_when_artifacts_present(self) -> None:
        diagnostic = classify_e2e_artifact_collection(
            configured_globs=[".issue-orchestrator/e2e-results/**/*.log"],
            collected_count=3,
        )
        assert diagnostic.state is E2EArtifactCollectionState.COLLECTED
        assert diagnostic.collected_count == 3
        assert diagnostic.configured_glob_count == 1

    def test_collected_takes_priority_even_without_configured_globs(self) -> None:
        # JUnit XML is always an artifact even if artifact_paths is empty, so a
        # positive collected_count must classify as collected regardless.
        diagnostic = classify_e2e_artifact_collection(
            configured_globs=[],
            collected_count=1,
        )
        assert diagnostic.state is E2EArtifactCollectionState.COLLECTED

    def test_globs_matched_nothing_when_configured_but_empty(self) -> None:
        diagnostic = classify_e2e_artifact_collection(
            configured_globs=[
                ".issue-orchestrator/e2e-results/**/*.log",
                ".issue-orchestrator/e2e-results/**/*.xml",
            ],
            collected_count=0,
        )
        assert diagnostic.state is E2EArtifactCollectionState.GLOBS_MATCHED_NOTHING
        assert diagnostic.collected_count == 0
        assert diagnostic.configured_glob_count == 2

    def test_not_configured_when_no_globs_and_no_artifacts(self) -> None:
        diagnostic = classify_e2e_artifact_collection(
            configured_globs=[],
            collected_count=0,
        )
        assert diagnostic.state is E2EArtifactCollectionState.NOT_CONFIGURED
        assert diagnostic.configured_glob_count == 0

    def test_blank_globs_are_not_counted_as_configured(self) -> None:
        # Trailing-newline YAML lists must not read as configured globs.
        diagnostic = classify_e2e_artifact_collection(
            configured_globs=["", "   ", "\n"],
            collected_count=0,
        )
        assert diagnostic.state is E2EArtifactCollectionState.NOT_CONFIGURED
        assert diagnostic.configured_glob_count == 0
