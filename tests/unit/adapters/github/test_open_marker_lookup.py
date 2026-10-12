"""The authoritative open-issue marker lookup (#7490)."""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.github.marker_recovery import prove_open_marker_issue


class FakeClient:
    """The client's complete, cursor-paged open-issue read (#8986)."""

    def __init__(self, bodies: dict[int, str]) -> None:
        self.bodies = bodies
        self.reads = 0

    def list_open_issue_bodies_complete(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return [(number, f"Issue {number}", (), body) for number, body in self.bodies.items()]


def test_it_reads_every_open_issue_body_whatever_its_title_or_labels() -> None:
    client = FakeClient({3: "x", 5: "<!-- m -->\nbody"})

    found = prove_open_marker_issue(client, marker="<!-- m -->")  # type: ignore[arg-type]

    assert found is not None and (found.number, found.title, found.body) == (5, "Issue 5", "<!-- m -->\nbody")
    assert client.reads == 1


def test_a_miss_is_none() -> None:
    assert prove_open_marker_issue(FakeClient({3: "x"}), marker="m") is None  # type: ignore[arg-type]


def test_an_empty_marker_is_refused() -> None:
    with pytest.raises(ValueError):
        prove_open_marker_issue(FakeClient({}), marker=" ")  # type: ignore[arg-type]
