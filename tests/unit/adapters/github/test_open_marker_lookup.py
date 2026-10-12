"""The authoritative open-issue marker lookup (#7490)."""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.github.marker_recovery import open_issue_bodies_containing, prove_open_marker_issue


class FakeClient:
    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        self.calls: list[dict] = []

    def list_issues(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return self.issues


def test_it_reads_the_open_labelled_issues_uncached_and_completely() -> None:
    client = FakeClient([{"number": 3, "body": "x"}, {"number": 5, "body": "<!-- m -->\nbody"}])

    found = prove_open_marker_issue(client, marker="<!-- m -->")  # type: ignore[arg-type]

    assert found is not None and found.number == 5
    [call] = client.calls
    assert "labels" not in call and call["state"] == "open"
    assert call["use_cache"] is False and call["exhaustive"] is True


def test_a_miss_is_none() -> None:
    assert prove_open_marker_issue(FakeClient([]), marker="m") is None  # type: ignore[arg-type]


def test_an_empty_marker_is_refused() -> None:
    with pytest.raises(ValueError):
        prove_open_marker_issue(FakeClient([]), marker=" ")  # type: ignore[arg-type]


def test_open_bodies_holding_a_needle_are_read_uncached_and_completely() -> None:
    """#8986: every open issue's body, whatever its title or labels."""
    client = FakeClient([
        {"number": 3, "body": "x"},
        {"number": 5, "body": "<!-- d:{} -->\nbody"},
        {"number": 7, "body": None},
        {"number": 9, "body": "notes\n<!-- d:{} -->"},
    ])

    found = open_issue_bodies_containing(client, needle="<!-- d:")  # type: ignore[arg-type]

    assert found == [(5, "<!-- d:{} -->\nbody"), (9, "notes\n<!-- d:{} -->")]
    [call] = client.calls
    assert "labels" not in call and call["state"] == "open"
    assert call["use_cache"] is False and call["exhaustive"] is True


def test_an_empty_needle_is_refused() -> None:
    with pytest.raises(ValueError):
        open_issue_bodies_containing(FakeClient([]), needle=" ")  # type: ignore[arg-type]
