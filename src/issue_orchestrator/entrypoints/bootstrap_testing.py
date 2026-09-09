"""Explicit test-composition defaults that never perform publication effects."""

from unittest.mock import MagicMock

from ..ports.issue_tracker import IssueTracker
from ..ports.manual_publication import ManualPublisher
from ..ports.event_sink import EventSink
from ..ports.session_runner import SessionRunner


class Dependencies:
    """Small compatibility container retained for explicit injection tests."""

    def __init__(
        self,
        events: EventSink,
        runner: SessionRunner,
        github: object | None = None,
    ) -> None:
        self.events = events
        self.runner = runner
        self.github = github


class TestingFreshIssueReader:
    """Read labels through the repository port supplied by a test."""

    def __init__(self, issue_tracker: IssueTracker) -> None:
        self._issue_tracker = issue_tracker

    def read_issue_labels(self, issue_number: int) -> list[str]:
        return self._issue_tracker.get_issue_labels(issue_number)


def manual_publication_for_testing(publisher: ManualPublisher | None) -> ManualPublisher:
    if publisher is not None:
        return publisher
    unconfigured = MagicMock(spec=ManualPublisher)
    unconfigured.publish.side_effect = AssertionError(
        "Manual publication tests must inject their behavior port"
    )
    return unconfigured
