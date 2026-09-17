"""Backend-agnostic contract assertions for the LaneExecutor port.

Every backend adapter must pass these unchanged: they define what
"callers cannot tell backends apart" means. Backend-specific test
modules instantiate :class:`LaneExecutorContract` with a factory for
their adapter and inherit the whole suite.

Waiting here is always waiting on an EVENT the system emits — a pid file
the fixture wrote, a marker on the lane's stdout, a pid the kernel has
stopped knowing about. Every ``*_BACKSTOP_SECONDS`` below exists only to
turn a hang into a failure that NAMES the event that never happened; none
of them is a coordination device, and no assertion is true because a
sleep was long enough (#7148).
"""

from __future__ import annotations

import _thread
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.domain.lane_execution import (
    LANE_TIMEOUT_EXIT_CODE,
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneOutcome,
    LaneResources,
    LaneTimedOut,
    LaneWorkKey,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.event_wait import POLL_SECONDS as _POLL_SECONDS, await_event as _await
from tests.load_fixture import reap_marked_processes

# --------------------------------------------------------------------------
# Backstops. Each one names the event whose absence it reports.
# --------------------------------------------------------------------------

# How long the tree fixture may take to reach its first instruction and
# announce itself. Deliberately generous, because this is precisely the
# window #7148 was about: on a slim CI container the interpreter's own
# start-up outran a 5s lane deadline, the fixture was killed before it
# wrote its pid file, and the test failed with a FileNotFoundError that
# named nothing. Startup speed is now a precondition, never a race.
_READINESS_BACKSTOP_SECONDS = 60.0

# How long a cancelled lane's process tree may take to disappear. Must
# exceed the scheduler backend's soft-to-hard kill grace
# (``job_max_vacate_time``, 10s) plus its polling latency.
_TREE_REAP_BACKSTOP_SECONDS = 60.0

# The deadline the deadline test submits. Nothing has to have happened by
# the time it fires, so it can be short.
_DEADLINE_UNDER_TEST_SECONDS = 5.0

# The streaming proof is TWO events, deliberately separated (#7264). The old
# single window could only ever report "the backend buffers", which was the
# less likely of the two things its expiry actually meant.
#
# First: how long the streaming lane may take to reach its FIRST FLUSH — it
# announces that itself, so this expiry means the lane never got that far. It is
# not a statement about buffering, and it is not a statement about output
# either: a lane killed between its print and its announcement leaves the same
# silence, which is why the assertion reports the announcement, not the bytes.
#
# QUEUE WAIT LANDS HERE. A scheduling backend's admission is explicitly outside
# the lane's deadline and may legitimately exceed it, so a queued job spends its
# wait in THIS window, and such a backend must raise
# ``LaneExecutorContract.first_flush_backstop_seconds`` (and the lane lifetime
# with it). The default suits a backend that starts its lane when asked.
_STREAM_FLUSH_BACKSTOP_SECONDS = 45.0
# Second: having been told the bytes were written, how long they may take to
# become observable on the parent's streams while the lane is provably still
# running. THIS expiry is the buffering diagnosis, and it is the only thing that
# can produce one.
#
# It is not a PROOF of buffering: a backend that relays through a poll loop of
# its own (the Condor executor's ``_OutputStreamer.pump``) is indistinguishable
# from one that buffers if that loop never runs. The window is sized so that
# only a relay which has stopped entirely can reach it, and the assertion says
# which two causes it cannot tell apart rather than asserting the likelier one.
_STREAM_OBSERVABLE_BACKSTOP_SECONDS = 45.0
# How long the lane may then take to conclude once the handshake releases it.
_LANE_CONCLUSION_BACKSTOP_SECONDS = 60.0

# --------------------------------------------------------------------------
# Fixture processes. Every one of them dies of its own clock (#7142): a
# ``finally`` protects only a harness that gets to run it, and the machine
# pays for the fixtures of harnesses that did not.
# --------------------------------------------------------------------------

# 300s is chosen against the observation windows above, not against comfort:
# every wait in this module is bounded well inside it, so this clock cannot
# end a fixture before the backend's duty has been observed — it can only
# stop one outliving the machine's next few gates.
_TREE_LIFETIME_SECONDS = 300.0
_SLEEPER_LIFETIME_SECONDS = 300.0
# The streaming lane waits for a handshake this test writes; the clock is
# what makes it safe when the handshake never comes. Declared as a constant
# and threaded through argv so the lifetime scan can see it — spelled as
# ``time.time() + 90`` inside the script it was invisible to the scan.
#
# It must outlive BOTH observation windows above (45 + 45, plus margin), or its
# own clock could end the lane mid-observation and the failure would name this
# fixture instead of the backend. Spelled as a literal because the lifetime scan
# reads literals; the arithmetic is asserted by
# ``test_the_streaming_lane_outlives_every_window_that_observes_it``.
_STREAMING_LANE_LIFETIME_SECONDS = 120.0

# A process tree that must be KILLED, never asked: both processes ignore
# SIGTERM, so only an escalation to SIGKILL (or a scheduler's hard kill of
# the job family) removes them. Ignoring SIGTERM is the point of this
# fixture; being immortal is not, so both hold one absolute expiry taken
# before the fork.
#
# Its FIRST act after forking is to announce itself: the child writes both
# pids and renames the record into place, so the file's existence is the
# readiness event and a reader can never see half of it. Everything that
# waits on this tree waits on that event.
_TREE_SCRIPT = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
deadline = time.monotonic() + float(sys.argv[2])
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    from pathlib import Path
    ready = Path(sys.argv[1])
    staged = ready.with_name(ready.name + '.staged')
    staged.write_text(str(os.getppid()) + '\\n' + str(os.getpid()) + '\\n')
    staged.replace(ready)
    while time.monotonic() < deadline:
        time.sleep(0.5)
    os._exit(0)
while time.monotonic() < deadline:
    time.sleep(0.5)
"""

# Prints one marker, then refuses to conclude until the test releases it —
# so the marker can only be observed while the lane is provably running.
#
# Its FIRST act after the flush is to ANNOUNCE the flush, the way the tree
# fixture announces its pids: the sentinel's existence is the event "this lane
# has written its output", and it is what lets the test tell "the lane never
# spoke" apart from "the lane spoke and the backend swallowed it" (#7264).
# ``print(flush=True)`` has returned by then, so the bytes are already on the
# inherited descriptor when the sentinel appears.
_STREAMING_SCRIPT = """
import sys, time, pathlib
print('STREAM-MARKER', flush=True)
pathlib.Path(sys.argv[3]).touch()
deadline = time.monotonic() + float(sys.argv[2])
while not pathlib.Path(sys.argv[1]).exists():
    if time.monotonic() > deadline:
        raise SystemExit(9)
    time.sleep(0.1)
"""

# A lane with nothing to establish: it is asleep from its first instruction,
# so how long it took to get there cannot change what the deadline does to
# it. Cooperative with SIGTERM on purpose — this fixture is for classifying
# an overrun, not for proving anything about kill topology.
_SLEEPER_SCRIPT = """
import sys, time
time.sleep(float(sys.argv[1]))
"""


@dataclass(frozen=True, slots=True)
class TreePids:
    """The tree fixture's own report of the tree it started."""

    parent: int
    grandchild: int


def read_tree_pids(readiness_path: Path) -> TreePids:
    """Read the readiness record :data:`_TREE_SCRIPT` writes.

    The one owner of that file's format, so the tests that wait on the
    event and the guardrail that re-asserts the fixture's TERM-immunity
    cannot drift apart.

    Raises:
        FileNotFoundError: the tree has not announced itself yet.
        ValueError: the content is not the two pids the fixture writes.
            Callers polling for the event retry on both.
    """
    fields = readiness_path.read_text().split()
    if len(fields) != 2:
        raise ValueError(f"{readiness_path} is not a tree readiness record: {fields!r}")
    return TreePids(parent=int(fields[0]), grandchild=int(fields[1]))


def _command(
    work_key: str,
    arguments: tuple[str, ...],
    working_directory: Path,
    timeout_seconds: float,
) -> LaneCommand:
    return LaneCommand(
        work_key=LaneWorkKey(work_key),
        arguments=arguments,
        working_directory=working_directory.resolve(),
        deadline=LaneDeadline(timeout_seconds),
    )


def _release_lane(handshake: Path, thread: threading.Thread) -> None:
    """Let the streaming lane conclude, and wait for it, whatever else failed.

    The lane is held alive by the ABSENCE of ``handshake``, so this cannot be
    skipped -- a leaked lane costs a queueing backend scheduler time its own
    deadline never bounds. Two rules beyond "always join":

    * a release failure never MASKS the failure it is cleaning up after. When
      something is already propagating, that exception is the causal one and
      this one is dropped to its ``__context__``;
    * when nothing is propagating, the release failure is the failure.
    """
    pending = sys.exc_info()[1]
    try:
        handshake.write_text("go")
    except OSError:
        if pending is None:
            raise
    finally:
        thread.join(timeout=_LANE_CONCLUSION_BACKSTOP_SECONDS)


def _await_pid_gone(pid: int, deadline_seconds: float) -> bool:
    def gone() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # The pid EXISTS and is simply not ours to signal. Not gone: keep
            # waiting, and report not-gone if the window closes on this.
            pass
        return False

    return _await(gone, backstop_seconds=deadline_seconds)


@dataclass(frozen=True, slots=True)
class _CancellationAttempt:
    """What happened when a running lane's caller was cancelled."""

    outcome: LaneOutcome | None
    cancelled: BaseException | None
    pids: TreePids | None
    readiness_timed_out: bool


class _CancelWhenTreeIsReady:
    """Cancel the lane when its process tree announces itself — or when
    the backstop says it never will.

    The backends' cancellation path is entered by an exception raised in
    the thread that called ``run()`` — an operator's Ctrl-C, a dying
    supervisor. There is no out-of-band cancel API to call, so this
    reproduces the real trigger: a watcher waits for the readiness event
    and then raises ``KeyboardInterrupt`` in the main thread with
    ``_thread.interrupt_main``. No signal is delivered to any process, and
    none to any other thread.

    A backstop that only stops WATCHING would leave the run to finish on
    somebody else's clock (round 1, #7148). A lane that is admitted and
    then never dispatched does not reach its own deadline — that clock
    starts at dispatch — so it runs until the backend's admission
    watchdog, ten minutes away, and the enclosing pytest timeout gets
    there first. The operator would then read a generic "test exceeded
    600s" for precisely the failure this suite exists to name. So the
    expiry cancels too: the run ends here, the named readiness assertion
    is what fails, and the lane's job is removed instead of sitting in a
    queue nobody is watching any more.

    Armed only while ``run()`` is in progress. If the lane instead
    concludes on its own — a regression, or the deadline backstop — the
    interrupt is never sent, so a failing test fails on its own assertions
    rather than throwing a KeyboardInterrupt into pytest.
    """

    def __init__(self, readiness_path: Path) -> None:
        self._readiness_path = readiness_path
        self._lock = threading.Lock()
        self._armed = False
        self._observed_pids: TreePids | None = None
        self._readiness_timed_out = False
        self._thread = threading.Thread(
            target=self._watch, name="lane-cancel-when-ready", daemon=True
        )

    @property
    def observed_pids(self) -> TreePids | None:
        """The tree the watcher saw, or None if readiness never arrived."""
        return self._observed_pids

    @property
    def readiness_timed_out(self) -> bool:
        """Whether the run was cancelled for never announcing a tree."""
        return self._readiness_timed_out

    def arm(self) -> None:
        self._armed = True
        self._thread.start()

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
        self._thread.join(timeout=_READINESS_BACKSTOP_SECONDS)
        if self._thread.is_alive():
            raise AssertionError(
                "the cancellation watcher outlived its own readiness "
                f"backstop of {_READINESS_BACKSTOP_SECONDS:.0f}s"
            )

    def _watch(self) -> None:
        def announced_or_disarmed() -> bool:
            with self._lock:
                if not self._armed:
                    return True
                pids = self._read_pids()
                if pids is None:
                    return False
                self._observed_pids = pids
                _thread.interrupt_main()
                return True

        if _await(
            announced_or_disarmed, backstop_seconds=_READINESS_BACKSTOP_SECONDS
        ):
            return
        # Nothing is ever going to announce itself. End the run on THIS
        # clock so the test's own assertion is the failure the operator
        # reads, and so the lane does not outlive the watcher.
        with self._lock:
            if not self._armed:
                return
            self._readiness_timed_out = True
            _thread.interrupt_main()

    def _read_pids(self) -> TreePids | None:
        try:
            return read_tree_pids(self._readiness_path)
        except (FileNotFoundError, ValueError):
            return None


def _cancel_when_ready(
    executor: LaneExecutor,
    command: LaneCommand,
    resources: LaneResources,
    readiness_path: Path,
) -> _CancellationAttempt:
    """Run the lane and cancel its caller once the tree is up."""
    trigger = _CancelWhenTreeIsReady(readiness_path)
    outcome: LaneOutcome | None = None
    cancelled: BaseException | None = None
    trigger.arm()
    try:
        outcome = executor.run(command, resources)
    except KeyboardInterrupt as interrupt:
        cancelled = interrupt
    finally:
        try:
            trigger.disarm()
        except KeyboardInterrupt as late:
            # Delivered in the microseconds between the watcher's armed
            # check and this disarm. It belongs to this attempt, not to
            # whatever pytest would have run next.
            cancelled = late if cancelled is None else cancelled
    return _CancellationAttempt(
        outcome, cancelled, trigger.observed_pids, trigger.readiness_timed_out
    )


class LaneExecutorContract:
    """Inherit and implement :meth:`build_executor` to adopt the suite."""

    # Generous machinery allowance: scheduling/startup overhead must
    # never be billed against the behavior under test.
    completion_timeout_seconds = 120.0

    # How long THIS backend may take to reach the streaming lane's first flush,
    # and how long the lane's own clock then gives it. A queueing backend raises
    # both; the constants above say why queue wait belongs to the first.
    first_flush_backstop_seconds = _STREAM_FLUSH_BACKSTOP_SECONDS
    streaming_lane_lifetime_seconds = _STREAMING_LANE_LIFETIME_SECONDS

    def build_executor(self) -> LaneExecutor:
        raise NotImplementedError

    def resources(self) -> LaneResources:
        return LaneResources(request_cpus=1)

    def test_completes_in_working_directory_with_environment(
        self, tmp_path: Path
    ) -> None:
        marker_variable = "LANE_CONTRACT_TOKEN"
        os.environ[marker_variable] = "lane-contract-proof"
        try:
            outcome = self.build_executor().run(
                _command(
                    "contract.completes",
                    (
                        sys.executable,
                        "-c",
                        "import os, pathlib; "
                        "pathlib.Path('lane-proof.txt').write_text("
                        f"os.environ['{marker_variable}'])",
                    ),
                    tmp_path,
                    self.completion_timeout_seconds,
                ),
                self.resources(),
            )
        finally:
            del os.environ[marker_variable]
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 0
        assert (tmp_path / "lane-proof.txt").read_text() == "lane-contract-proof"

    def test_nonzero_exit_code_propagates_exactly(self, tmp_path: Path) -> None:
        outcome = self.build_executor().run(
            _command(
                "contract.exit-code",
                (sys.executable, "-c", "raise SystemExit(17)"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 17

    def test_observed_runtime_reflects_actual_execution(self, tmp_path: Path) -> None:
        """Completed lanes report how long they actually executed.

        The lower bound proves the value tracks real execution; the
        upper bound is the machinery allowance, deliberately loose —
        precision belongs to the backends, plausibility to the
        contract. Queue-wait exclusion is proven where queues exist
        (the scheduler backend's integration suite)."""
        outcome = self.build_executor().run(
            _command(
                "contract.runtime",
                (sys.executable, "-c", "import time; time.sleep(2)"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 0
        assert (
            1.5 <= outcome.observed_runtime_seconds <= (self.completion_timeout_seconds)
        )

    def test_queue_wait_is_reported_and_plausible(self, tmp_path: Path) -> None:
        """Completed lanes price their scheduling wait separately.

        No upper bound on purpose (B3, #7122 review): queue wait is
        explicitly excluded from the lane's deadline and may
        legitimately exceed it under pool contention — capping it by
        the runtime allowance would fail this shared contract for
        correct behavior. The contract asks only that the field is
        reported non-negative; the direct backend's exact zero and the
        scheduler backend's real waits are backend-suite facts."""
        outcome = self.build_executor().run(
            _command(
                "contract.queue-wait",
                (sys.executable, "-c", "pass"),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.queue_wait_seconds >= 0.0

    def test_the_streaming_lane_outlives_every_window_that_observes_it(self) -> None:
        """The fixture's own clock may never end an observation in progress.

        If it could, a slow runner would kill the lane mid-window and the
        failure would name this fixture rather than the backend under test --
        which is the class of confusion #7264 was filed about. Asserted rather
        than commented because both numbers are per-backend and the lifetime is
        spelled as a literal for the fixture-lifetime scan, so nothing else keeps
        them in step. It runs once per backend, against THAT backend's numbers.
        """
        windows = (
            self.first_flush_backstop_seconds + _STREAM_OBSERVABLE_BACKSTOP_SECONDS
        )

        assert self.streaming_lane_lifetime_seconds > windows, (
            "the streaming lane can expire while the test is still watching it: "
            f"lifetime={self.streaming_lane_lifetime_seconds:.0f}s vs windows "
            f"{self.first_flush_backstop_seconds:.0f}s + "
            f"{_STREAM_OBSERVABLE_BACKSTOP_SECONDS:.0f}s"
        )

    def test_output_streams_before_the_lane_completes(
        self, tmp_path: Path, capfd: "pytest.CaptureFixture[str]"
    ) -> None:
        """The port promises STREAMED output, not buffered-until-done.

        The lane prints a marker, announces that it flushed it, and then
        refuses to exit until this test writes a handshake file. Observing the
        marker on the parent's streams while the lane is provably still running
        is the streaming proof.

        TWO events, not one clock (#7264). The previous version polled ``capfd``
        for 60s and blamed the backend for buffering when the poll came up
        empty -- but the identical observation is produced by a lane that never
        ran, and on a loaded runner with 12 xdist workers that is the likelier
        cause. The lane now says for itself when it has written its output, so
        the two are separated: the flush window expiring means the lane never
        spoke, and only the observation window expiring accuses the backend.
        """
        handshake = tmp_path / "proceed"
        flushed = tmp_path / "flushed"
        outcomes: list[object] = []

        def run_lane() -> None:
            outcomes.append(
                self.build_executor().run(
                    _command(
                        "contract.streaming",
                        (
                            sys.executable,
                            "-c",
                            _STREAMING_SCRIPT,
                            str(handshake),
                            str(self.streaming_lane_lifetime_seconds),
                            str(flushed),
                        ),
                        tmp_path,
                        self.completion_timeout_seconds,
                    ),
                    self.resources(),
                )
            )

        thread = threading.Thread(target=run_lane)
        thread.start()
        try:
            lane_announced_flush = _await(
                flushed.exists,
                backstop_seconds=self.first_flush_backstop_seconds,
            )
            observed = ""

            def marker_observed() -> bool:
                nonlocal observed
                captured = capfd.readouterr()
                observed += captured.out + captured.err
                return "STREAM-MARKER" in observed

            marker_seen = _await(
                marker_observed,
                backstop_seconds=_STREAM_OBSERVABLE_BACKSTOP_SECONDS,
            )
            lane_still_running = thread.is_alive()
        finally:
            _release_lane(handshake, thread)

        # The first-flush answer comes first: it is the precondition for reading
        # anything into the other two, and a lane still queued at the end would
        # otherwise be reported as one that failed to conclude.
        assert lane_announced_flush, (
            "the lane never announced its first flush within "
            f"{self.first_flush_backstop_seconds:.0f}s. This is NOT a buffering "
            "diagnosis and NOT proof that no output was written: the lane may "
            "never have been admitted or scheduled, or may have been killed "
            "between writing the marker and announcing it. Nothing here is a "
            "statement about the backend's streaming."
        )
        assert marker_seen or lane_still_running, (
            "the lane concluded before its output was ever observed, although "
            "the handshake that releases it had not been written - the "
            "fixture's own clock or a backend cancellation ended it, so nothing "
            "here says anything about streaming. (A lane that concludes AFTER "
            "the marker is observed is fine: the streaming duty was already "
            "discharged.)"
        )
        assert marker_seen, (
            "the lane announced that it flushed STREAM-MARKER, and it was still "
            f"not observable on the parent's streams "
            f"{_STREAM_OBSERVABLE_BACKSTOP_SECONDS:.0f}s later while the lane was "
            "still running. Either the backend buffers until completion, or a "
            "relay it pumps on its own loop stopped running; this test cannot "
            "tell those two apart, so check the backend's relay before "
            "concluding it buffers."
        )
        assert not thread.is_alive(), (
            "the lane never concluded within "
            f"{_LANE_CONCLUSION_BACKSTOP_SECONDS:.0f}s of being released by "
            "the handshake"
        )
        assert outcomes and type(outcomes[0]) is LaneCompleted
        assert outcomes[0].exit_code == 0

    def test_signal_death_reports_as_128_plus_signal(self, tmp_path: Path) -> None:
        outcome = self.build_executor().run(
            _command(
                "contract.signal-death",
                (
                    sys.executable,
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                ),
                tmp_path,
                self.completion_timeout_seconds,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneCompleted
        assert outcome.exit_code == 137

    def test_deadline_overrun_is_reported_as_timed_out(self, tmp_path: Path) -> None:
        """What the deadline uniquely owns here: the classification.

        The workload has nothing to establish before it can be killed — it
        is asleep from its first instruction — so how long it took to get
        there cannot change the outcome, only when it happens. That is the
        whole point of splitting this test (#7148): the previous version
        proved the classification AND the reaping of a process tree in one
        run, which made it a race between the lane's 5s deadline and the
        job's own start-up. It lost that race twice in CI.

        Reaping descendants is proven by the cancellation test below,
        against the same kill mechanism this deadline reaches (the direct
        backend calls one containment routine from both paths; the
        scheduler backend removes the job either way — a periodic
        expression and ``condor_rm`` produce the same abort event, and the
        classifier tells them apart only by the reason text).
        """
        outcome = self.build_executor().run(
            _command(
                "contract.deadline",
                (
                    sys.executable,
                    "-c",
                    _SLEEPER_SCRIPT,
                    str(_SLEEPER_LIFETIME_SECONDS),
                ),
                tmp_path,
                _DEADLINE_UNDER_TEST_SECONDS,
            ),
            self.resources(),
        )
        assert type(outcome) is LaneTimedOut, (
            "a lane still sleeping past its "
            f"{_DEADLINE_UNDER_TEST_SECONDS:.0f}s deadline ended as "
            f"{outcome!r}"
        )
        assert outcome.exit_code == LANE_TIMEOUT_EXIT_CODE

    def test_cancelling_a_running_lane_reaps_its_whole_process_tree(
        self, tmp_path: Path
    ) -> None:
        """A cancelled lane takes its descendants with it — on purpose.

        Three separate things, in order, each waited on as an event:

        1. The tree announces itself (both pids, one atomic record). Until
           that has happened there is nothing to kill, so the lane's own
           deadline is a generous backstop rather than the trigger.
        2. The kill is triggered deliberately, through the cancellation
           path the backends actually implement: an exception in the
           thread that called ``run()``.
        3. Both processes — the parent and its TERM-immune grandchild —
           stop existing. Neither cooperates with SIGTERM, so nothing here
           passes because a process was polite.
        """
        # The interrupt must be raised in the main thread. The test owns the
        # handler for the duration of its trigger: validation wrappers and
        # test runners may legitimately install a process-wide SIGINT handler,
        # and inheriting that unrelated state made this contract order-dependent.
        assert threading.current_thread() is threading.main_thread(), (
            "this test cancels by interrupting the main thread, so it must "
            "be the thread that called into the backend"
        )
        previous_interrupt_handler = signal.signal(
            signal.SIGINT, signal.default_int_handler
        )
        readiness_path = tmp_path / "tree-ready.pids"
        try:
            attempt = _cancel_when_ready(
                self.build_executor(),
                _command(
                    "contract.cancel-tree",
                    (
                        sys.executable,
                        "-c",
                        _TREE_SCRIPT,
                        str(readiness_path),
                        str(_TREE_LIFETIME_SECONDS),
                    ),
                    tmp_path,
                    self.completion_timeout_seconds,
                ),
                self.resources(),
                readiness_path,
            )
            assert attempt.pids is not None, (
                "the lane never announced a running process tree at "
                f"{readiness_path} within {_READINESS_BACKSTOP_SECONDS:.0f}s. "
                + (
                    "The run was cancelled at that backstop so THIS is the "
                    "failure you are reading, rather than the enclosing test "
                    "timeout: the lane was still running (queued, or started "
                    "and mute) with nothing to kill."
                    if attempt.readiness_timed_out
                    else f"The lane concluded on its own first, as {attempt.outcome!r}."
                )
            )
            assert attempt.cancelled is not None, (
                "the backend swallowed its caller's cancellation and "
                f"returned {attempt.outcome!r} instead of re-raising"
            )
            for role, pid in (
                ("parent", attempt.pids.parent),
                ("grandchild", attempt.pids.grandchild),
            ):
                assert _await_pid_gone(pid, _TREE_REAP_BACKSTOP_SECONDS), (
                    f"a TERM-immune {role} survived the lane's cancellation "
                    f"by {_TREE_REAP_BACKSTOP_SECONDS:.0f}s: pid={pid}"
                )
        finally:
            signal.signal(signal.SIGINT, previous_interrupt_handler)
            # #7142: ``_TREE_SCRIPT`` ignores SIGTERM, so exactly when the
            # assertions above are doing their job — a backend regressed,
            # or the tree never announced itself — this test is the thing
            # leaving signal-resistant processes on the machine. Five of
            # them, up to twelve hours old, were found here. The pgid
            # belongs to the backend, so identity comes from the argv.
            reap_marked_processes(str(readiness_path))
