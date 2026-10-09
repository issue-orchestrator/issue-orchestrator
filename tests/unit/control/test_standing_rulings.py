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

    assert section is not None and COVERED_RULINGS_HEADING in section
    assert f"{RULINGS_PROMPT_HEADING}364 (PR #12)" in section and ruling.text in section
    assert "365" not in section and "#13" not in section
    assert sorted(bodies.reads) == [364, 365] and index.rows == {364: (ruling,), 365: ()}
    assert owner.covered_section({365: (13,)}) is None and owner.covered_section({}) is None


def test_a_covered_number_github_has_no_issue_for_binds_nothing_but_a_failed_read_refuses() -> None:
    """A PR branch named ``999-...`` with no issue #999 must not wedge every batch
    review; a read that FAILS, or a damaged block, still refuses the section."""
    from issue_orchestrator.ports.repository_host import RepositoryHostError

    ruling = a_ruling()
    bodies = IssueBodies({364: body_with(ruling), 366: body_with(ruling).replace("<!-- io:standing-rulings:end -->", "")})
    bodies.unreadable.add(999)
    bodies.failing.add(365)
    owner = rulings_owner(bodies)

    section = owner.covered_section({364: (12,), 999: (12,)})

    assert section is not None and ruling.text in section and "#999" not in section
    with pytest.raises(RepositoryHostError):
        owner.covered_section({364: (12,), 365: ()})
    with pytest.raises(StandingRulingsUnavailable, match="#366"):
        owner.covered_section({366: (13,)})


def _listed(number: int, body: str | None) -> SimpleNamespace:
    return SimpleNamespace(number=number, body=body)


def test_backfill_indexes_rulings_recorded_before_the_index_existed() -> None:
    """porchpin#364/#327: rulings on the body before the index was created never
    reached the page until something read the issue again. A listed issue with
    no rulings and no row costs no read."""
    ruling = a_ruling()
    bodies = IssueBodies({364: body_with(ruling), 327: body_with(ruling), 5: SPEC})
    index = InMemoryStandingRulingsIndex()
    owner = rulings_owner(bodies, index)

    filled = owner.backfill([_listed(n, bodies.bodies[n]) for n in (364, 327, 5)])

    assert filled == 2 and index.rows == {364: (ruling,), 327: (ruling,)}
    assert set(owner.synced()) == {364, 327} and sorted(bodies.reads) == [327, 364]


def test_backfill_catches_a_hand_edit_on_an_issue_already_synced() -> None:
    """A row synced empty, then a maintainer added a ruling on GitHub by hand:
    the listing differs from the row, so the issue is re-read and indexed."""
    ruling = a_ruling()
    bodies = IssueBodies({364: body_with(ruling)})
    index = InMemoryStandingRulingsIndex({364: ()})

    assert rulings_owner(bodies, index).backfill([_listed(364, bodies.bodies[364])]) == 1
    assert index.rows == {364: (ruling,)}


def test_backfill_never_trusts_a_stale_listing_over_github() -> None:
    """The listing was read before a ruling was retired: it differs from the
    row, and the fresh read (the truth) keeps it retired. A damaged block is
    left to the reads that refuse it; a failing GitHub stops the backfill."""
    old = a_ruling("m-000000000001")
    damaged = body_with(old).replace("<!-- io:standing-rulings:end -->", "")
    bodies = IssueBodies({364: SPEC, 9: damaged, 10: SPEC})
    bodies.failing.add(11)
    index = InMemoryStandingRulingsIndex({364: ()})
    owner = rulings_owner(bodies, index)

    filled = owner.backfill([
        _listed(364, body_with(old)), _listed(9, damaged), _listed(11, body_with(old)), _listed(12, body_with(old)),
    ])

    assert filled == 1 and index.rows == {364: ()}
    assert bodies.reads == [364, 9, 11]  # stopped at the failing read: 12 waits for the next startup


def test_a_retry_reads_the_recorded_covered_work_fresh() -> None:
    """codex r3 F2 / r4 F2: the issues a retry is bound by are the ones its
    launch recorded (a triage item's included, a PR's links edited since
    notwithstanding); their rulings are read fresh, so one recorded since binds."""
    from issue_orchestrator.control.launch_prompt import IssueLaunchPrompt
    from issue_orchestrator.control.tech_lead_covered_rulings import covered_issues, retried_covered_rulings
    from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor
    from issue_orchestrator.ports.coder_prompt import NO_CODER_PROMPT_ADDENDUM

    covered = covered_issues({364: (12,)}, (), frozenset({365, 950}), anchor=950)
    assert covered == {364: (12,), 365: ()}  # the anchor's own come with its launch prompt
    authority = TechLeadLaunchAuthority.from_dict(TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW, anchor_issue_number=950, manifest_pr_numbers=(12,),
        covered_work=tuple(sorted(covered.items())),
    ).to_dict())
    since = a_ruling("m-00000000beef", "Recorded after the launch.")
    bodies = IssueBodies({364: body_with(since), 365: body_with(since)})

    section = retried_covered_rulings(
        IssueLaunchPrompt(NO_CODER_PROMPT_ADDENDUM, rulings_owner(bodies)), SimpleNamespace(authority=authority),
    )

    assert authority.covered_work == ((364, (12,)), (365, ()))
    assert section is not None and since.text in section and "issue #364 (PR #12)" in section
    assert sorted(bodies.reads) == [364, 365]


def test_covered_work_must_be_the_manifests_and_never_the_anchor() -> None:
    from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor

    for covered_work in (((364, (13,)),), ((950, ()),), ((365, ()), (364, ()))):
        with pytest.raises(ValueError, match="covered work"):
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.BATCH_REVIEW, anchor_issue_number=950, manifest_pr_numbers=(12,),
                covered_work=covered_work,
            )
