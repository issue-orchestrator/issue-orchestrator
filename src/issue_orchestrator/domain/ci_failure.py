"""What a failed CI job's log says about the failure (#8692).

Pure vocabulary for the CI-failure triage owner
(``control/ci_failure_triage.py``): the classification of one job's log
against the configured signatures, the bounded excerpt a rework brief carries,
and the durable PR-comment marker that records a transient re-run, so the
engine re-runs a head commit's failed jobs at most once, across restarts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

#: Runner and infrastructure failures that say nothing about the code: a
#: re-run on the same commit is the right first response. Matched
#: case-insensitively, line by line, after the Actions timestamp prefix is
#: stripped. Operators extend this with known-flaky signatures in config.
DEFAULT_TRANSIENT_SIGNATURES: tuple[str, ...] = (
    r"The runner has received a shutdown signal",
    r"lost communication with the server",
    r"The hosted runner encountered an error",
    r"has exceeded the maximum execution time",
    r"The job was not acquired by Runner",
    r"No space left on device",
    r"Failed to download action",
    r"Could not resolve host",
    r"\b(?:502 Bad Gateway|503 Service Unavailable|504 Gateway Time-?out)\b",
    r"API rate limit exceeded",
    r"The operation was canceled",
)

#: Test, assertion, compile and type failures: the code is wrong, so the log
#: goes to the coding agent. Checked only after no transient signature matched.
DEFAULT_GENUINE_SIGNATURES: tuple[str, ...] = (
    r"AssertionError",
    r"^FAILED \S",
    r"^=+ .*\b\d+ failed\b",
    r"^--- FAIL:",
    r"^\s*FAIL\s",
    r"\bTests?:\s+\d+ failed\b",
    r"npm ERR! Test failed",
    r"error\[E\d{4}\]",
    r"error TS\d+:",
    r"\bpanicked at\b",
    r"Traceback \(most recent call last\)",
    r"^E\s+\S",
)

#: A check conclusion that is itself the transient signature: the job hit its
#: time ceiling, or the runner never started it.
TRANSIENT_CONCLUSIONS: frozenset[str] = frozenset({"TIMED_OUT", "STARTUP_FAILURE"})

#: How much of one job's log tail a rework brief carries.
EXCERPT_MAX_LINES = 60
EXCERPT_MAX_CHARS = 4000

_ACTIONS_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?", re.MULTILINE)


class CiFailureKind(Enum):
    TRANSIENT = "transient"
    GENUINE = "genuine"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CiFailureSignatures:
    """The compiled signature lists one classification runs against."""

    transient: tuple[re.Pattern[str], ...]
    genuine: tuple[re.Pattern[str], ...]

    @classmethod
    def compile(
        cls, transient: Sequence[str], genuine: Sequence[str]
    ) -> "CiFailureSignatures":
        """Compile both lists, failing loudly on an invalid pattern."""
        flags = re.IGNORECASE | re.MULTILINE
        return cls(
            transient=tuple(re.compile(p, flags) for p in transient),
            genuine=tuple(re.compile(p, flags) for p in genuine),
        )


@dataclass(frozen=True)
class CiJobAssessment:
    """One failed job, its log excerpt, and what the excerpt says."""

    name: str
    conclusion: str
    job_id: int | None
    run_id: int | None
    kind: CiFailureKind
    #: The signature that decided ``kind`` (a pattern, or ``conclusion:X``);
    #: ``None`` for an unknown failure.
    signature: str | None
    excerpt: str
    #: Why the log could not be read, when it could not.
    unreadable: str | None = None


def normalize_log(text: str) -> str:
    """Strip the Actions per-line timestamp so signatures can anchor on ``^``."""
    return _ACTIONS_TIMESTAMP.sub("", text)


def classify_job_log(
    conclusion: str, log: str, signatures: CiFailureSignatures
) -> tuple[CiFailureKind, str | None]:
    """Classify one failed job: transient, genuine, or unknown.

    Transient wins: a runner death can follow a test failure, but a re-run
    costs one CI cycle and is spent at most once per head commit, while
    misreading an infra failure as genuine costs a coding agent's rework cycle.
    """
    if conclusion.upper() in TRANSIENT_CONCLUSIONS:
        return CiFailureKind.TRANSIENT, f"conclusion:{conclusion.upper()}"
    for pattern in signatures.transient:
        if pattern.search(log):
            return CiFailureKind.TRANSIENT, pattern.pattern
    for pattern in signatures.genuine:
        if pattern.search(log):
            return CiFailureKind.GENUINE, pattern.pattern
    return CiFailureKind.UNKNOWN, None


def log_excerpt(log: str) -> str:
    """The last lines of a normalized log, bounded in lines and characters."""
    lines = log.rstrip("\n").splitlines()[-EXCERPT_MAX_LINES:]
    text = "\n".join(lines)
    if len(text) > EXCERPT_MAX_CHARS:
        text = text[-EXCERPT_MAX_CHARS:]
    return text


def overall_kind(assessments: Sequence[CiJobAssessment]) -> CiFailureKind:
    """Transient only when every failed job is; genuine when any job is."""
    kinds = {a.kind for a in assessments}
    if not kinds:
        return CiFailureKind.UNKNOWN
    if kinds == {CiFailureKind.TRANSIENT}:
        return CiFailureKind.TRANSIENT
    if CiFailureKind.GENUINE in kinds:
        return CiFailureKind.GENUINE
    return CiFailureKind.UNKNOWN


# --------------------------------------------------------------------------- #
# The durable re-run record
# --------------------------------------------------------------------------- #

RERUN_MARKER_PREFIX = "<!-- io:ci-rerun "
_RERUN_MARKER = re.compile(
    r"<!-- io:ci-rerun head=(?P<head>[0-9a-f]{7,64}) jobs=(?P<jobs>[\d,]*) "
    r"at=(?P<at>\S+) -->"
)


@dataclass(frozen=True)
class CiRerunRecord:
    """One transient re-run the engine requested, as its PR comment records it."""

    head_sha: str
    job_ids: frozenset[int]
    requested_at: datetime


def rerun_marker(head_sha: str, job_ids: Sequence[int], at: datetime) -> str:
    jobs = ",".join(str(j) for j in sorted(job_ids))
    return f"{RERUN_MARKER_PREFIX}head={head_sha} jobs={jobs} at={at.isoformat()} -->"


def parse_rerun_records(bodies: Sequence[str]) -> tuple[CiRerunRecord, ...]:
    """Every re-run record in the PR's comments; a malformed marker fails loudly."""
    records: list[CiRerunRecord] = []
    for body in bodies:
        for line in body.splitlines():
            if not line.startswith(RERUN_MARKER_PREFIX):
                continue
            match = _RERUN_MARKER.fullmatch(line.strip())
            if match is None:
                raise ValueError(f"malformed CI re-run marker: {line!r}")
            records.append(
                CiRerunRecord(
                    head_sha=match["head"],
                    job_ids=frozenset(int(j) for j in match["jobs"].split(",") if j),
                    requested_at=datetime.fromisoformat(match["at"]),
                )
            )
    return tuple(records)
