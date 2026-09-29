"""The label-scoped, authoritative open-issue marker lookup (#7490)."""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.github.marker_recovery import prove_open_labelled_marker_issue


class FakeClient:
    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        self.calls: list[dict] = []

    def list_issues(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return self.issues


def test_it_reads_the_open_labelled_issues_uncached_and_completely() -> None:
    client = FakeClient([{"number": 3, "body": "x"}, {"number": 5, "body": "<!-- m -->\nbody"}])

    found = prove_open_labelled_marker_issue(client, label="improver", marker="<!-- m -->")  # type: ignore[arg-type]

    assert found is not None and found.number == 5
    [call] = client.calls
    assert call["labels"] == ["improver"] and call["state"] == "open"
    assert call["use_cache"] is False and call["exhaustive"] is True


def test_a_miss_is_none() -> None:
    assert prove_open_labelled_marker_issue(FakeClient([]), label="improver", marker="m") is None  # type: ignore[arg-type]


def test_an_empty_marker_or_label_is_refused() -> None:
    with pytest.raises(ValueError):
        prove_open_labelled_marker_issue(FakeClient([]), label="", marker="m")  # type: ignore[arg-type]
