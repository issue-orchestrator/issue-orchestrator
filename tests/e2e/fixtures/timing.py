"""E2E timing infrastructure for tracking test and phase durations."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Phase:
    """Track timing for a phase within a test."""
    name: str
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start if self.end else time.time() - self.start


@dataclass
class TestTiming:
    """Track timing for a single test, including phases."""
    name: str
    start: float = 0.0
    end: float = 0.0
    phases: list[Phase] = field(default_factory=list)
    status: str = "running"  # running, passed, failed

    @property
    def duration(self) -> float:
        return self.end - self.start if self.end else time.time() - self.start

    def start_phase(self, name: str) -> Phase:
        phase = Phase(name=name, start=time.time())
        self.phases.append(phase)
        return phase

    def end_phase(self, phase: Phase) -> None:
        phase.end = time.time()

    def print_phases(self, indent: str = "    ") -> None:
        """Print phase breakdown for this test."""
        if not self.phases:
            return
        for p in self.phases:
            pct = (p.duration / self.duration * 100) if self.duration > 0 else 0
            print(f"{indent}{p.name}: {p.duration:.1f}s ({pct:.0f}%)")


@dataclass
class E2ETimingStats:
    """Track timing for the entire e2e test session."""
    session_start: float = field(default_factory=time.time)
    test_timings: list[TestTiming] = field(default_factory=list)
    current_test: TestTiming | None = None
    detailed: bool = True  # Always show phase breakdown by default

    def start_test(self, name: str) -> None:
        self.current_test = TestTiming(name=name, start=time.time())

    def end_test(self, status: str = "passed") -> TestTiming | None:
        if self.current_test:
            self.current_test.end = time.time()
            self.current_test.status = status
            self.test_timings.append(self.current_test)
            result = self.current_test
            self.current_test = None
            return result
        return None

    @contextmanager
    def phase(self, name: str):
        """Context manager to track a phase within the current test.

        Usage:
            with e2e_timing_stats.phase("Creating issue"):
                issue = create_issue(...)
        """
        if self.current_test:
            p = self.current_test.start_phase(name)
            try:
                yield
            finally:
                self.current_test.end_phase(p)
        else:
            yield  # No-op if not in a test

    @property
    def total_duration(self) -> float:
        return time.time() - self.session_start

    def print_summary(self, detailed: bool | None = None) -> None:
        """Print timing summary. Use detailed=True for phase breakdown."""
        show_detailed = detailed if detailed is not None else self.detailed
        print("\n" + "=" * 70)
        print("E2E TEST TIMING SUMMARY")
        print("=" * 70)
        for t in self.test_timings:
            if t.status == "passed":
                status = "+" if t.duration < 120 else "!"
            else:
                status = "x"
            print(f"  {status} {t.name}: {t.duration:.1f}s [{t.status}]")
            if show_detailed and t.phases:
                t.print_phases()
        print("-" * 70)
        passed = sum(1 for t in self.test_timings if t.status == "passed")
        failed = sum(1 for t in self.test_timings if t.status == "failed")
        print(f"  TOTAL: {self.total_duration:.1f}s ({self.total_duration/60:.1f} min)")
        print(f"  Tests: {len(self.test_timings)} ({passed} passed, {failed} failed)")
        if self.test_timings:
            avg = sum(t.duration for t in self.test_timings) / len(self.test_timings)
            print(f"  Average: {avg:.1f}s per test")
        print("=" * 70)
