"""The payload of ``issue.labels_changed``: its one writer and its readers agree here.

The action applier publishes it for every label it adds or removes; the
improver's blocked-items staging reads it back from the timeline to date when
a block began (#7490).
"""

from __future__ import annotations

#: Set (True) on an add whose presence read failed: the label may already
#: have been on, so the add is not proven to be the moment it went on.
PRESENCE_UNKNOWN = "presence_unknown"


def labels_changed_payload(
    issue_number: int, issue_key: str, added: list[str], removed: list[str], *, presence_unknown: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "issue_number": issue_number,
        "issue_key": issue_key or str(issue_number),
        "added": added,
        "removed": removed,
    }
    if presence_unknown:
        payload[PRESENCE_UNKNOWN] = True
    return payload


__all__ = ["PRESENCE_UNKNOWN", "labels_changed_payload"]
