"""Fakes of GitHub's label events for the shared needs-human block (#8774).

The block owner binds each generation of ``needs-human`` to the ``labeled``
event standing on GitHub. :class:`LabelEvents` is a fake issue that records a
new event every time a label goes on afresh, so a person's hand removal and
re-application is visible exactly as it is on GitHub.
:func:`standing_while_present` answers from a test's own label read, with one
event per number for as long as the label stands, for a test that is not
about re-application.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from issue_orchestrator.domain.tech_lead_approval import LabelEvent


def label_event(event_id: int, created_at: str | None = None) -> LabelEvent:
    """A ``labeled`` event with GitHub's required fields."""
    return LabelEvent(
        event_id=event_id, actor_login="engine[bot]", actor_is_bot=True,
        created_at=created_at or f"2026-10-08T00:00:{event_id % 60:02d}Z", actor_id=9,
    )


class LabelEvents:
    """Live labels, and the event that applied each label standing now."""

    def __init__(self) -> None:
        self.live: dict[int, set[str]] = {}
        self.applied: dict[tuple[int, str], LabelEvent] = {}
        self._events = 0
        #: Set to make the events read fail, as an unreadable timeline does.
        self.events_unreadable = False
        #: How much later than now the next label write is dated.
        self.later = timedelta()

    def add_label(self, issue_number: int, label: str) -> None:
        if label not in self.live.setdefault(issue_number, set()):
            self._events += 1
            dated = (datetime.now(timezone.utc) + self.later).isoformat().replace("+00:00", "Z")
            self.applied[(issue_number, label)] = label_event(self._events, dated)
        self.live[issue_number].add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).discard(label)
        self.applied.pop((issue_number, label), None)

    def reapply_by_hand(self, issue_number: int, label: str) -> None:
        """A person takes ``label`` off and puts it back between observations."""
        self.remove_label(issue_number, label)
        self.add_label(issue_number, label)

    def read_labels(self, issue_number: int) -> list[str]:
        return sorted(self.live.get(issue_number, set()))

    def label_application(self, issue_number: int, label: str) -> LabelEvent | None:
        if self.events_unreadable:
            raise RuntimeError("issue events unreadable")
        return self.applied.get((issue_number, label))

    def label_applications(
        self, issue_number: int, labels: Sequence[str]
    ) -> dict[str, LabelEvent | None]:
        return {label: self.label_application(issue_number, label) for label in labels}


def standing_while_present(
    read_labels: Callable[[int], Sequence[str]],
) -> Callable[[int, str], LabelEvent | None]:
    """One application per number while ``read_labels`` shows the label on it."""

    def application(issue_number: int, label: str) -> LabelEvent | None:
        return label_event(issue_number) if label in read_labels(issue_number) else None

    return application


__all__ = ["LabelEvents", "label_event", "standing_while_present"]
