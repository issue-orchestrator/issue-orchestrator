"""Pytest live-lane verdict: missing coverage and quota exhaustion are not green."""

import json
import os
from pathlib import Path

import pytest

UNAVAILABLE_MESSAGES = (
    "you've hit your usage limit", "you’ve hit your usage limit",
    "you've hit your limit", "you’ve hit your limit", "weekly limit",
    "credit balance is too low", "insufficient_quota", "rate_limit_exceeded",
)


class LiveReport:
    def __init__(self, output: Path | None) -> None:
        self.output = output
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.unavailable: set[str] = set()
        self.selected = 0

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.selected = len(session.items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.unavailable.add(report.nodeid)
        elif report.failed:
            evidence = (str(report.longrepr) + report.capstdout + report.capstderr).lower()
            if any(message in evidence for message in UNAVAILABLE_MESSAGES):
                self.unavailable.add(report.nodeid)
            else:
                self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.unavailable or not self.selected or exitstatus not in {0, 1}:
            status = "unavailable"
        elif self.failed:
            status = "failed"
        elif exitstatus or len(self.passed) != self.selected:
            status = "unavailable"
        else:
            status = "passed"
        if status == "unavailable":
            session.exitstatus = 75
        elif status != "passed" and not exitstatus:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        payload = {
            "status": status, "selected": self.selected,
            "passed": sorted(self.passed), "failed": sorted(self.failed),
            "unavailable": sorted(self.unavailable),
        }
        if self.output:
            self.output.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nLive-agent coverage: {status} ({len(self.passed)}/{self.selected} passed)")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--agent-test-report", help="Write a separate live-agent verdict JSON")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("numprocesses", default=0):
        raise pytest.UsageError("Live-agent verdicts require serial execution (-n 0)")
    output = config.getoption("agent_test_report") or os.environ.get("IO_BUDGETED_VALIDATION_RESULT")
    config.pluginmanager.register(LiveReport(Path(output) if output else None), "live-agent-verdict")
