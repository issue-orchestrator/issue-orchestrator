"""The CI-failure classifier and its durable re-run record (#8692)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from issue_orchestrator.domain.ci_failure import (
    DEFAULT_GENUINE_SIGNATURES,
    DEFAULT_TRANSIENT_SIGNATURES,
    EXCERPT_MAX_CHARS,
    EXCERPT_MAX_LINES,
    CiFailureKind,
    CiFailureSignatures,
    CiJobAssessment,
    classify_job_log,
    log_excerpt,
    normalize_log,
    overall_kind,
    escalated_heads,
    parse_rerun_records,
    rerun_escalated_marker,
    rerun_marker,
)
from issue_orchestrator.infra.config_models import CiFailureTriageConfig

DEFAULTS = CiFailureSignatures.compile(DEFAULT_TRANSIENT_SIGNATURES, DEFAULT_GENUINE_SIGNATURES)

RUNNER_LOST = """\
2026-10-08T05:59:01.1234567Z ##[group]Run cargo test --workspace
2026-10-08T06:31:44.0000000Z ##[error]The hosted runner: GitHub Actions 7 lost communication with the server. Anything in your workflow that terminates the runner process, starves it for CPU/Memory, or blocks its network access can cause this error.
"""
PYTEST_FAILED = """\
2026-10-08T06:01:02.0000000Z tests/test_watch.py::test_process_tree FAILED
2026-10-08T06:01:02.1000000Z E       AssertionError: expected 3 children, saw 2
2026-10-08T06:01:03.0000000Z FAILED tests/test_watch.py::test_process_tree - AssertionError
2026-10-08T06:01:03.1000000Z ========================= 1 failed, 212 passed in 31.20s =========================
2026-10-08T06:01:03.2000000Z ##[error]Process completed with exit code 1.
"""
OPAQUE = """\
2026-10-08T06:01:03.0000000Z make: *** [lint] Error 2
2026-10-08T06:01:03.2000000Z ##[error]Process completed with exit code 2.
"""


def _classify(conclusion: str, raw: str, signatures: CiFailureSignatures = DEFAULTS):
    return classify_job_log(conclusion, normalize_log(raw), signatures)


def test_runner_loss_is_transient_and_names_its_signature() -> None:
    kind, signature = _classify("FAILURE", RUNNER_LOST)
    assert kind is CiFailureKind.TRANSIENT
    assert signature == r"lost communication with the server"


def test_test_failure_is_genuine_even_with_line_timestamps() -> None:
    kind, signature = _classify("FAILURE", PYTEST_FAILED)
    assert kind is CiFailureKind.GENUINE
    assert signature == "AssertionError"


def test_anchored_genuine_signature_matches_after_timestamp_strip() -> None:
    log = "2026-10-08T06:01:03.0000000Z --- FAIL: TestWatch (0.01s)\n"
    assert _classify("FAILURE", log) == (CiFailureKind.GENUINE, r"^--- FAIL:")
    # The raw (unstripped) line must not satisfy the anchored pattern.
    assert classify_job_log("FAILURE", log, DEFAULTS)[0] is CiFailureKind.UNKNOWN


def test_unrecognised_failure_is_unknown() -> None:
    assert _classify("FAILURE", OPAQUE) == (CiFailureKind.UNKNOWN, None)


@pytest.mark.parametrize("conclusion", ["TIMED_OUT", "STARTUP_FAILURE", "timed_out"])
def test_job_ceiling_conclusions_are_transient_whatever_the_log(conclusion: str) -> None:
    kind, signature = _classify(conclusion, PYTEST_FAILED)
    assert kind is CiFailureKind.TRANSIENT
    assert signature == f"conclusion:{conclusion.upper()}"


def test_transient_wins_over_genuine_in_one_log() -> None:
    kind, _ = _classify("FAILURE", PYTEST_FAILED + RUNNER_LOST)
    assert kind is CiFailureKind.TRANSIENT


def test_operator_known_flaky_signature_makes_a_test_failure_transient() -> None:
    config = CiFailureTriageConfig(
        transient_signatures=[*DEFAULT_TRANSIENT_SIGNATURES, r"test_watch\.py::test_process_tree"]
    )
    signatures = CiFailureSignatures.compile(config.transient_signatures, config.genuine_signatures)
    assert _classify("FAILURE", PYTEST_FAILED, signatures) == (
        CiFailureKind.TRANSIENT, r"test_watch\.py::test_process_tree",
    )


def test_invalid_signature_fails_config_loudly() -> None:
    with pytest.raises(ValueError, match="invalid regular expression"):
        CiFailureTriageConfig(transient_signatures=["(unclosed"])


def test_excerpt_is_the_bounded_tail() -> None:
    log = "\n".join(f"line {n}" for n in range(500))
    excerpt = log_excerpt(log)
    assert excerpt.splitlines()[-1] == "line 499"
    assert len(excerpt.splitlines()) == EXCERPT_MAX_LINES
    wide = "\n".join("x" * 1000 for _ in range(20))
    assert len(log_excerpt(wide)) == EXCERPT_MAX_CHARS


def _assessment(kind: CiFailureKind) -> CiJobAssessment:
    return CiJobAssessment("job", "FAILURE", 1, 2, kind, None, "")


def test_overall_kind_is_transient_only_when_every_job_is() -> None:
    t, g, u = CiFailureKind.TRANSIENT, CiFailureKind.GENUINE, CiFailureKind.UNKNOWN
    assert overall_kind([_assessment(t), _assessment(t)]) is t
    assert overall_kind([_assessment(t), _assessment(u)]) is u
    assert overall_kind([_assessment(t), _assessment(g), _assessment(u)]) is g
    assert overall_kind([]) is u


def test_rerun_marker_round_trips_and_rejects_a_malformed_one() -> None:
    at = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
    body = rerun_marker("a" * 40, [12, 11], at) + "\nio re-ran ..."
    (record,) = parse_rerun_records([body, "unrelated comment"])
    assert record.head_sha == "a" * 40
    assert record.job_ids == frozenset({11, 12})
    assert record.requested_at == at
    with pytest.raises(ValueError, match="malformed"):
        parse_rerun_records(["<!-- io:ci-rerun head=zz -->"])


def test_escalated_heads_are_read_back_from_the_escalation_comment() -> None:
    body = f"**Diagnosis:** io asked GitHub ... {rerun_escalated_marker('a' * 40)}"
    assert escalated_heads([body, "nothing"]) == frozenset({"a" * 40})
    # An escalation marker is not a malformed re-run record.
    assert parse_rerun_records([body]) == ()
