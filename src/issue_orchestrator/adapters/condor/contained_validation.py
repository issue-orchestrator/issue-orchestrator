# pyright: strict
"""Crash-recoverable Condor ownership for budgeted live validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from ...domain.lane_execution import (
    LaneCommand, LaneDeadline, LaneResources, LaneSuspendability, LaneWorkKey,
)
from ...ports.command_runner import CommandResult
from ...ports.contained_validation import (
    ContainedValidationCommand, ContainedValidationPending,
)
from .event_classifier import (
    LaneJobDeadlineRemoved, LaneJobExited, LaneJobFaulted, LaneJobKilledBySignal,
    LaneJobPending, LaneJobRemoved, LaneJobRunning, LaneJobSuspended,
    classify_event_log,
)
from .submit_compiler import CompiledSubmitDescription, compile_submit_description
from .tools import CondorTools, LaneExecutorError

_RECEIPT = "containment.json"
_CONTAINMENT = "contained-job"
_ATTESTATION = "IO_EXECENV_CGROUP_CONTAINMENT"
_HISTORY_DIRECTORY = "PER_JOB_HISTORY_DIR"
_JOB_FROM_EVENT = re.compile(r"\((\d+)\.(\d+)\.\d+\)")
_JOB_ID = re.compile(r"^\d+\.\d+$")
_HISTORY_OPERATION = re.compile(
    r'^IssueOrchestratorOperationId\s*=\s*"([A-Za-z0-9._-]+)"\s*$', re.MULTILINE,
)
_POLL_SECONDS = 0.1
_QUEUE_ALLOWANCE_SECONDS = 600
_SCHEDULER_SLACK_SECONDS = 120


@dataclass(frozen=True, slots=True)
class _Reservation:
    operation_id: str
    fingerprint: str
    phase: str
    job_id: str | None = None
    result: dict[str, object] | None = None


class UnavailableContainedValidationRunner:
    """A safe unavailable result for hosts without verified containment."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def reserved(self, evidence_directory: Path) -> bool:
        return (evidence_directory / _RECEIPT).is_file()

    def run(self, command: ContainedValidationCommand) -> CommandResult:
        if self.reserved(command.evidence_directory):
            raise ContainedValidationPending(
                "this host cannot reconcile the existing contained job reservation"
            )
        return CommandResult(75, "", self._reason)


class CondorContainedValidationRunner:
    """Persist intent before submit and release only after final ClassAd proof."""

    def __init__(self, tools: CondorTools) -> None:
        self._tools = tools
        attestation = tools.read_configuration(_ATTESTATION)
        if attestation.returncode != 0 or attestation.stdout.rstrip("\r\n") != "True":
            raise RuntimeError(
                "budgeted live validation requires the Linux execenv cgroup pool"
            )
        history = tools.read_configuration(_HISTORY_DIRECTORY)
        if history.returncode != 0 or not history.stdout.rstrip("\r\n"):
            raise RuntimeError(
                "budgeted live validation requires per-job final ClassAds"
            )
        self._history_directory = Path(history.stdout.rstrip("\r\n"))
        if not self._history_directory.is_absolute():
            raise RuntimeError("per-job final ClassAd directory must be absolute")

    def reserved(self, evidence_directory: Path) -> bool:
        return (evidence_directory / _RECEIPT).is_file()

    def run(self, command: ContainedValidationCommand) -> CommandResult:
        receipt_path = command.evidence_directory / _RECEIPT
        fingerprint = _fingerprint(command)
        if receipt_path.is_file():
            reservation = _read_reservation(receipt_path)
            if reservation.operation_id != command.operation_id or reservation.fingerprint != fingerprint:
                raise RuntimeError("contained validation reservation does not match its request")
        else:
            reservation = self._prepare(command, fingerprint)
        if reservation.phase == "finished":
            return _decode_result(reservation)
        if reservation.phase == "prepared":
            reservation = _replace_reservation(reservation, phase="submitting")
            _write_reservation(receipt_path, reservation)
            reservation = self._submit(command, reservation)
        elif reservation.phase == "submitting":
            reservation = self._reconcile_ambiguous_submission(command, reservation)
        if reservation.phase != "submitted" or reservation.job_id is None:
            raise ContainedValidationPending(
                "scheduler ownership is unresolved; reservation retained"
            )
        return self._follow(command, reservation)

    def _prepare(self, command: ContainedValidationCommand,
                 fingerprint: str) -> _Reservation:
        run_directory = command.evidence_directory / _CONTAINMENT
        if run_directory.exists():
            # No receipt exists, so submission intent was never persisted and
            # this preparation directory cannot belong to a scheduler job.
            shutil.rmtree(run_directory)
        run_directory.mkdir()
        lane = LaneCommand(
            LaneWorkKey(f"bval-{command.operation_id[:48]}"),
            command.arguments,
            command.working_directory.resolve(),
            LaneDeadline(float(command.timeout_seconds)),
        )
        compiled = compile_submit_description(
            lane,
            LaneResources(
                request_cpus=1,
                exclusive=("budgeted_live_agent",),
                request_memory_mb=4096,
                suspendability=LaneSuspendability.NEVER,
            ),
            run_directory.resolve(),
            operation_identity=command.operation_id,
            max_wall_seconds=(
                command.timeout_seconds
                + _QUEUE_ALLOWANCE_SECONDS
                + _SCHEDULER_SLACK_SECONDS
            ),
        )
        compiled.exec_script_path.write_text(compiled.exec_script_text, encoding="utf-8")
        compiled.exec_script_path.chmod(0o755)
        (run_directory / "lane.sub").write_text(compiled.text, encoding="utf-8")
        reservation = _Reservation(command.operation_id, fingerprint, "prepared")
        _write_reservation(command.evidence_directory / _RECEIPT, reservation)
        return reservation

    def _submit(self, command: ContainedValidationCommand,
                reservation: _Reservation) -> _Reservation:
        submit_path = command.evidence_directory / _CONTAINMENT / "lane.sub"
        try:
            completed = self._tools.invoke(
                (str(self._tools.submit), "-terse", str(submit_path)),
                environment=command.environment,
            )
        except LaneExecutorError as error:
            raise ContainedValidationPending(
                f"submission acknowledgement unavailable: {error}"
            ) from error
        tokens = completed.stdout.split()
        if completed.returncode != 0 or not tokens:
            raise ContainedValidationPending(
                "submission outcome is ambiguous; reservation retained"
            )
        if _JOB_ID.fullmatch(tokens[0]) is None:
            raise RuntimeError("scheduler returned an invalid validation job identifier")
        reservation = _replace_reservation(
            reservation, phase="submitted", job_id=tokens[0],
        )
        _write_reservation(command.evidence_directory / _RECEIPT, reservation)
        return reservation

    def _reconcile_ambiguous_submission(
        self, command: ContainedValidationCommand, reservation: _Reservation,
    ) -> _Reservation:
        job_ids = self._query_job_ids(command.operation_id)
        if len(job_ids) > 1:
            raise RuntimeError("multiple scheduler jobs share one validation operation")
        job_id = job_ids[0] if job_ids else self._job_id_from_event(command)
        if job_id is None:
            raise ContainedValidationPending(
                "no authoritative submit outcome yet; refusing duplicate submission"
            )
        reservation = _replace_reservation(
            reservation, phase="submitted", job_id=job_id,
        )
        _write_reservation(command.evidence_directory / _RECEIPT, reservation)
        return reservation

    def _follow(self, command: ContainedValidationCommand,
                reservation: _Reservation) -> CommandResult:
        compiled = _compiled_paths(command.evidence_directory)
        local_deadline = time.monotonic() + (
            command.timeout_seconds
            + _QUEUE_ALLOWANCE_SECONDS
            + _SCHEDULER_SLACK_SECONDS
            + 30
        )
        while True:
            if time.monotonic() >= local_deadline:
                raise ContainedValidationPending(
                    "scheduler has not yet proved the bounded job family empty"
                )
            state = _read_state(compiled)
            if type(state) in (LaneJobPending, LaneJobRunning, LaneJobSuspended):
                time.sleep(_POLL_SECONDS)
                continue
            if self._query_job_ids(command.operation_id):
                time.sleep(_POLL_SECONDS)
                continue
            assert reservation.job_id is not None
            history = self._history_directory / f"history.{reservation.job_id}"
            if not history.is_file():
                raise ContainedValidationPending(
                    "terminal event observed but final ClassAd containment proof is pending"
                )
            match = _HISTORY_OPERATION.search(history.read_text(encoding="utf-8"))
            if match is None:
                raise ContainedValidationPending(
                    "final ClassAd has not recorded the operation identity yet"
                )
            if match.group(1) != command.operation_id:
                raise RuntimeError("final ClassAd belongs to another validation operation")
            stdout = _read_optional(compiled.output_path)
            stderr = _read_optional(compiled.error_path)
            result = _terminal_result(state, stdout, stderr)
            finished = _replace_reservation(
                reservation, phase="finished", result={
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "timed_out": result.timed_out,
                },
            )
            _write_reservation(command.evidence_directory / _RECEIPT, finished)
            return result

    def _query_job_ids(self, operation_id: str) -> tuple[str, ...]:
        constraint = f'IssueOrchestratorOperationId == "{operation_id}"'
        try:
            completed = self._tools.invoke((
                str(self._tools.query), "-allusers", "-constraint", constraint,
                "-json", "-attributes", "ClusterId,ProcId",
            ))
        except LaneExecutorError as error:
            raise ContainedValidationPending(
                f"scheduler query unavailable: {error}"
            ) from error
        if completed.returncode != 0:
            raise ContainedValidationPending("scheduler query did not establish ownership")
        try:
            ads = json.loads(completed.stdout or "[]")
            return tuple(f"{int(ad['ClusterId'])}.{int(ad['ProcId'])}" for ad in ads)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("scheduler returned malformed ownership records") from error

    @staticmethod
    def _job_id_from_event(command: ContainedValidationCommand) -> str | None:
        event_path = command.evidence_directory / _CONTAINMENT / "lane.events"
        try:
            match = _JOB_FROM_EVENT.search(event_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return f"{int(match.group(1))}.{int(match.group(2))}" if match else None


def _fingerprint(command: ContainedValidationCommand) -> str:
    payload = json.dumps({
        "operation_id": command.operation_id,
        "arguments": command.arguments,
        "working_directory": str(command.working_directory.resolve()),
        "timeout_seconds": command.timeout_seconds,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_reservation(path: Path) -> _Reservation:
    raw = json.loads(path.read_text())
    if raw.pop("version", None) != 1:
        raise ValueError("unrecognised contained validation reservation")
    return _Reservation(**raw)


def _replace_reservation(reservation: _Reservation, **changes: object) -> _Reservation:
    values = asdict(reservation)
    values.update(changes)
    return _Reservation(**values)


def _write_reservation(path: Path, reservation: _Reservation) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump({"version": 1, **asdict(reservation)}, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _compiled_paths(evidence: Path) -> CompiledSubmitDescription:
    run = evidence / _CONTAINMENT
    return CompiledSubmitDescription(
        text="", exec_script_path=run / "lane.exec", exec_script_text="",
        output_path=run / "lane.out", error_path=run / "lane.err",
        event_log_path=run / "lane.events", rusage_path=run / "lane.rusage",
    )


def _read_state(compiled: CompiledSubmitDescription):
    try:
        return classify_event_log(compiled.event_log_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LaneJobPending()


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _terminal_result(state: object, stdout: str, stderr: str) -> CommandResult:
    if type(state) is LaneJobExited:
        return CommandResult(state.exit_code, stdout, stderr)
    if type(state) is LaneJobKilledBySignal:
        return CommandResult(128 + state.signal_number, stdout, stderr)
    if type(state) is LaneJobDeadlineRemoved:
        return CommandResult(-9, stdout, stderr, True)
    if type(state) is LaneJobRemoved:
        return CommandResult(75, stdout, f"{stderr}\n{state.detail}")
    if type(state) is LaneJobFaulted:
        return CommandResult(75, stdout, f"{stderr}\n{state.detail}")
    raise RuntimeError("contained validation terminal state is not terminal")


def _decode_result(reservation: _Reservation) -> CommandResult:
    if reservation.result is None:
        raise ValueError("finished containment reservation lacks a result")
    raw = reservation.result
    returncode = raw.get("returncode")
    stdout = raw.get("stdout")
    stderr = raw.get("stderr")
    timed_out = raw.get("timed_out")
    if (type(returncode) is not int or type(stdout) is not str
            or type(stderr) is not str or type(timed_out) is not bool):
        raise ValueError("finished containment reservation has a malformed result")
    return CommandResult(
        returncode, stdout, stderr, timed_out,
    )
