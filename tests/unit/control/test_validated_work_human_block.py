"""Independent failed records share one label without sharing release authority."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from issue_orchestrator.control.needs_human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanBlock,
    NeedsHumanCause,
    ValidatedWorkBlockSource,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)


@dataclass
class Labels:
    current: set[str] = field(default_factory=set)
    removed: list[str] = field(default_factory=list)
    fail_remove: bool = False
    fail_read: bool = False

    def read(self, issue_number: int) -> list[str]:
        assert issue_number == 7
        if self.fail_read:
            raise OSError("remote unavailable")
        return sorted(self.current)

    def add_label(self, issue_number: int, label: str) -> None:
        assert issue_number == 7
        self.current.add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        assert issue_number == 7
        if self.fail_remove:
            raise OSError("remote unavailable")
        self.current.discard(label)
        self.removed.append(label)


def request(record: str) -> HumanBlockRequest:
    return HumanBlockRequest(
        7,
        NeedsHumanCause.VALIDATED_WORK_DISPOSITION,
        "validated publication failed",
        ValidatedWorkBlockSource(record),
    )


def owner(labels: Labels, path: Path) -> NeedsHumanBlock:
    return NeedsHumanBlock(
        "needs-human",
        "tech-lead-needs-human",
        labels,
        labels.read,
        frozenset,
        SqlitePendingWorkClaimStore(path),
    )


def test_two_failed_records_survive_restart_and_release_independently(
    tmp_path: Path,
) -> None:
    labels = Labels()
    path = tmp_path / "causes.sqlite"
    block = owner(labels, path)
    assert block.acquire(request("first")) is BlockOutcome.HELD
    assert block.acquire(request("second")) is BlockOutcome.HELD
    assert block.acquire(request("second")) is BlockOutcome.HELD
    restarted = owner(labels, path)
    assert restarted.release(request("first")) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert labels.current == {"needs-human"}
    assert labels.removed == []
    assert restarted.release(request("second")) is BlockOutcome.CLEARED
    assert labels.current == set()
    assert labels.removed == ["needs-human"]
    assert SqlitePendingWorkClaimStore(path).needs_human_causes(7) == frozenset()


@pytest.mark.parametrize(
    "other", [NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE]
)
def test_recovery_preserves_independent_lifecycle_cause(
    tmp_path: Path, other: NeedsHumanCause
) -> None:
    labels = Labels()
    block = owner(labels, tmp_path / "causes.sqlite")
    block.acquire(request("recovery"))
    block.acquire(HumanBlockRequest(7, other, "separate blocker"))
    assert block.release(request("recovery")) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert labels.current == {"needs-human"}


def test_ordinary_release_and_force_clear_cannot_clear_failed_disposition(
    tmp_path: Path,
) -> None:
    labels = Labels()
    block = owner(labels, tmp_path / "causes.sqlite")
    block.acquire(request("recovery"))
    ordinary = HumanBlockRequest(7, NeedsHumanCause.MERGE_ESCALATION, "old PR")
    block.acquire(ordinary)
    assert block.release(ordinary) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert block.force_clear(7, "retry") is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert block.unsettleable_holders(7) == (
        NeedsHumanCause.VALIDATED_WORK_DISPOSITION,
    )
    assert labels.removed == []


def test_last_release_preserves_ambiguous_label_write_with_source_intact(
    tmp_path: Path,
) -> None:
    labels = Labels(fail_remove=True)
    path = tmp_path / "causes.sqlite"
    block = owner(labels, path)
    block.acquire(request("recovery"))
    assert block.release(request("recovery")) is BlockOutcome.FAILED
    assert SqlitePendingWorkClaimStore(path).needs_human_causes(7) == frozenset(
        {request("recovery").cause_key}
    )
    labels.fail_remove = False
    assert owner(labels, path).release(request("recovery")) is BlockOutcome.FAILED
    assert labels.current == {"needs-human"}
    assert not labels.removed
    labels.current.clear()  # Its owner explicitly resolves the ambiguous label.
    assert owner(labels, path).release(request("recovery")) is BlockOutcome.CLEARED


def test_missing_source_never_removes_operator_label(tmp_path: Path) -> None:
    labels = Labels(current={"needs-human"})
    block = owner(labels, tmp_path / "causes.sqlite")
    assert block.release(request("unrecorded")) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert labels.removed == []


def test_label_generation_reset_requires_reprojection_of_retained_failed_records(
    tmp_path: Path,
) -> None:
    labels = Labels()
    path = tmp_path / "causes.sqlite"
    block = owner(labels, path)
    block.acquire(request("first"))
    block.acquire(request("second"))
    labels.current.clear()  # Human removes the label out of band.
    assert block.release(request("first")) is BlockOutcome.CLEARED
    assert SqlitePendingWorkClaimStore(path).needs_human_causes(7) == frozenset()
    # Disposition's next aggregate projection reasserts retained FAILED records;
    # ordinary cause rows still obey their existing label-generation semantics.
    block.acquire(request("second"))
    assert block.release(request("first")) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert labels.current == {"needs-human"}


def test_unreadable_issue_refuses_scoped_release(tmp_path: Path) -> None:
    labels = Labels()
    block = owner(labels, tmp_path / "causes.sqlite")
    block.acquire(request("recovery"))
    labels.fail_read = True
    assert block.release(request("recovery")) is BlockOutcome.FAILED
    assert labels.removed == []


def test_record_source_required_only_for_disposition_cause() -> None:
    with pytest.raises(ValueError, match="record source"):
        HumanBlockRequest(7, NeedsHumanCause.VALIDATED_WORK_DISPOSITION, "missing")
    with pytest.raises(ValueError, match="record source"):
        HumanBlockRequest(
            7,
            NeedsHumanCause.SESSION_LIFECYCLE,
            "wrong",
            ValidatedWorkBlockSource("record"),
        )
    with pytest.raises(ValueError, match="record_id"):
        ValidatedWorkBlockSource("")


def test_removal_intent_migrates_and_ends_with_its_cause_generation(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    path = tmp_path / "causes.sqlite"
    store = SqlitePendingWorkClaimStore(path)
    store.record_needs_human_cause(7, "first", reason="retained")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE needs_human_removal_intent")
    reopened = SqlitePendingWorkClaimStore(path)
    assert reopened.needs_human_causes(7) == frozenset({"first"})
    assert reopened.begin_needs_human_removal(7)
    reopened = SqlitePendingWorkClaimStore(path)
    assert not reopened.begin_needs_human_removal(7)
    reopened.restart_needs_human_causes(7, "second", reason="new generation")
    assert reopened.needs_human_causes(7) == frozenset({"second"})
    assert reopened.begin_needs_human_removal(7)
    reopened.clear_needs_human_causes(7)
    assert not reopened.needs_human_causes(7)
    assert reopened.begin_needs_human_removal(7)
