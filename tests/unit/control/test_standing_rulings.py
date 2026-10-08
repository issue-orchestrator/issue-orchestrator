"""The owner of an issue's standing rulings (#8141): GitHub body + local index."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.standing_rulings import RecordOutcome, RetireOutcome
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.standing_ruling import (
    REWORK_BRIEF_OPENING,
    RULINGS_BLOCK_BEGIN,
    parse_rulings_block,
)
from issue_orchestrator.infra.standing_rulings_store import SqliteStandingRulingsIndex
from issue_orchestrator.ports.standing_rulings import StandingRulingsUnavailable
from tests.standing_ruling_helpers import (
    InMemoryStandingRulingsIndex,
    IssueBodies,
    a_ruling,
    body_with,
    rulings_owner,
)

SPEC = "## Outcome\n\nThe maintainer's spec."


def test_recording_puts_the_ruling_at_the_top_of_the_body_and_in_the_index() -> None:
    bodies = IssueBodies({364: SPEC})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)
    ruling = a_ruling()

    assert owner.record(364, ruling) is RecordOutcome.RECORDED

    assert bodies.bodies[364].startswith(RULINGS_BLOCK_BEGIN) and bodies.bodies[364].endswith(SPEC)
    assert parse_rulings_block(bodies.bodies[364]) == (ruling,)
    assert index.rows[364] == (ruling,)


def test_recording_is_create_once_by_id_so_a_replay_writes_nothing() -> None:
    bodies = IssueBodies({364: SPEC})
    owner = rulings_owner(bodies)
    owner.record(364, a_ruling())

    assert owner.record(364, a_ruling(text="A replay with other words.")) is RecordOutcome.ALREADY_RECORDED
    assert len(bodies.writes) == 1


def test_a_second_ruling_keeps_the_first_and_a_hand_edited_spec() -> None:
    bodies = IssueBodies({364: SPEC})
    owner = rulings_owner(bodies)
    first, second = a_ruling("m-000000000001"), a_ruling("m-000000000002", "Second.")
    owner.record(364, first)
    bodies.bodies[364] = bodies.bodies[364] + "\n\nA maintainer's later note."

    owner.record(364, second)

    assert parse_rulings_block(bodies.bodies[364]) == (first, second)
    assert bodies.bodies[364].endswith("A maintainer's later note.")


def test_a_lost_index_is_hydrated_from_the_body_github_is_the_truth() -> None:
    ruling = a_ruling()
    bodies = IssueBodies({364: body_with(ruling)})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)

    assert owner.active(364) == (ruling,)
    assert index.rows[364] == (ruling,)


def test_a_hand_edit_or_another_engines_write_is_seen_at_the_next_read() -> None:
    """The index never stands in for the body: a ruling retired (or added) on
    GitHub by anyone else binds the very next prompt."""
    first, second = a_ruling("m-000000000001"), a_ruling("m-000000000002", "Second.")
    bodies = IssueBodies({364: body_with(first)})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)
    assert owner.active(364) == (first,)

    bodies.bodies[364] = body_with(second)
    section = owner.prompt_section(364, SessionKind.CODE)

    assert section is not None and "m-000000000002" in section and "m-000000000001" not in section
    assert index.rows[364] == (second,)
    bodies.bodies[364] = SPEC
    assert owner.prompt_section(364, SessionKind.CODE) is None and index.synced() == {}
    assert bodies.reads == [364, 364, 364]  # one fresh read per question


def test_an_unreadable_issue_or_damaged_block_is_unavailable_never_empty() -> None:
    bodies = IssueBodies({364: body_with(a_ruling()).replace("<!-- io:standing-rulings:end -->", "")})
    bodies.unreadable.add(365)
    owner = rulings_owner(bodies)

    with pytest.raises(StandingRulingsUnavailable, match="rulings-block"):
        owner.active(364)
    with pytest.raises(StandingRulingsUnavailable, match="could not be read"):
        owner.active(365)
    with pytest.raises(StandingRulingsUnavailable):
        owner.record(364, a_ruling("m-000000000009"))
    assert bodies.writes == []


def test_retiring_takes_it_off_the_body_and_the_index() -> None:
    bodies = IssueBodies({364: SPEC})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)
    owner.record(364, a_ruling())

    assert owner.retire(364, "m-0123456789ab") is RetireOutcome.RETIRED
    assert owner.retire(364, "m-0123456789ab") is RetireOutcome.NOT_FOUND
    assert bodies.bodies[364] == SPEC and index.rows[364] == ()


def test_every_rework_carries_the_rulings_as_its_brief_and_a_coder_as_constraints() -> None:
    owner = rulings_owner(IssueBodies({364: SPEC}))
    owner.record(364, a_ruling())

    rework = owner.prompt_section(364, SessionKind.REWORK)
    coding = owner.prompt_section(364, SessionKind.CODE)

    assert rework is not None and REWORK_BRIEF_OPENING in rework
    assert coding is not None and REWORK_BRIEF_OPENING not in coding and "Build to these rulings" in coding


def test_an_issue_without_rulings_has_no_prompt_section() -> None:
    assert rulings_owner(IssueBodies({1: SPEC})).prompt_section(1, SessionKind.CODE) is None


def test_the_sqlite_index_survives_an_engine_restart(tmp_path: Path) -> None:
    path = tmp_path / "standing_rulings.sqlite"
    ruling = a_ruling(files=("tools/walk",), claims=("retired",))
    first = SqliteStandingRulingsIndex(path)
    first.save(364, (ruling,))
    first.save(365, ())

    restarted = SqliteStandingRulingsIndex(path)

    assert restarted.load(364) == (ruling,)
    assert restarted.load(365) == () and restarted.load(366) is None
    [(number, synced)] = restarted.synced().items()
    assert number == 364 and synced.rulings == (ruling,) and synced.synced_at.startswith("20")


# -- #8347: rulings on the work a tech-lead run covers; the index backfill ------


def test_a_tech_lead_run_is_bound_by_each_covered_issues_rulings_read_fresh() -> None:
    """A batch review of PR #12 (issue 364) and PR #13 (issue 365, no rulings)
    is told 364's ruling and which PR it binds; each body is read fresh."""
    from issue_orchestrator.domain.standing_ruling import COVERED_RULINGS_HEADING, RULINGS_PROMPT_HEADING

    ruling = a_ruling(files=("tools/walk",))
    bodies = IssueBodies({364: body_with(ruling), 365: SPEC})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)

    section = owner.covered_section({364: (12,), 365: (13,)})

    assert section is not None and section.startswith(COVERED_RULINGS_HEADING)
    assert f"{RULINGS_PROMPT_HEADING}364 (PR #12)" in section and ruling.text in section
    assert "365" not in section and "#13" not in section
    assert sorted(bodies.reads) == [364, 365] and index.rows == {364: (ruling,), 365: ()}
    assert owner.covered_section({365: (13,)}) is None and owner.covered_section({}) is None


def test_a_covered_issue_that_cannot_be_read_refuses_the_whole_section() -> None:
    bodies = IssueBodies({364: body_with(a_ruling())})
    bodies.unreadable.add(365)

    with pytest.raises(StandingRulingsUnavailable, match="#365"):
        rulings_owner(bodies).covered_section({364: (12,), 365: ()})


def _listed(number: int, body: str | None) -> SimpleNamespace:
    return SimpleNamespace(number=number, body=body)


def test_backfill_indexes_rulings_recorded_before_the_index_existed() -> None:
    """porchpin#364/#327: rulings on the body before the index was created never
    reached the page until something read the issue again."""
    ruling = a_ruling()
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(IssueBodies(), index)

    filled = owner.backfill([_listed(364, body_with(ruling)), _listed(327, body_with(ruling)), _listed(5, SPEC)])

    assert filled == 2 and index.rows == {364: (ruling,), 327: (ruling,)}
    assert set(owner.synced()) == {364, 327}


def test_backfill_never_overwrites_a_synced_row_and_skips_a_damaged_block() -> None:
    """A row the owner synced is kept current by its own reads and writes (a
    ruling retired since the listing stays retired); a damaged block is left to the fresh
    reads that refuse it, without stopping the rest."""
    old, new = a_ruling("m-000000000001"), a_ruling("m-000000000002", "Newer.")
    index = InMemoryStandingRulingsIndex({364: ()})
    owner = rulings_owner(IssueBodies(), index)
    damaged = body_with(old).replace("<!-- io:standing-rulings:end -->", "")

    filled = owner.backfill([_listed(364, body_with(old)), _listed(9, damaged), _listed(10, body_with(new))])

    assert filled == 1 and index.rows == {364: (), 10: (new,)}
