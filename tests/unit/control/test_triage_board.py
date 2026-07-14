"""Tests for the rung-1 triage board projection + publisher (#6781).

The board is an orchestrator-authored projection of the triage ledgers: a
frozen view built from data the tick already holds (zero GitHub calls) and a
deterministic markdown renderer the publisher throttles by content comparison.
"""

from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.control.triage_board import (
    TRIAGE_BOARD_FILENAME,
    TriageBoardPublisher,
    triage_board_path,
)
from issue_orchestrator.domain.models import TriageFacts
from issue_orchestrator.domain.triage_session import (
    StoredTriageOp,
    TriageCaseFileSummary,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.triage_authority import InMemoryTriageAuthorityStore
from issue_orchestrator.view_models.triage_board import (
    TriageBoardCaseFile,
    TriageBoardProposal,
    TriageBoardView,
    _proposal_age_hours,
    build_triage_board_view,
    render_triage_board_md,
)

UTC = timezone.utc


def _op(target: int = 13, *, op_type: str = "reset_retry", created_at: str) -> StoredTriageOp:
    return StoredTriageOp(
        op_type=op_type,
        target_issue_number=target,
        rationale="r",
        source_run_id="run-1",
        source_session_name="issue-99",
        source_action_id="A2",
        created_at=created_at,
    )


def _summary(number: int, *, comments: int = 0, updated_at: str = "", area: str = "") -> TriageCaseFileSummary:
    return TriageCaseFileSummary(
        issue_number=number,
        title=f"Pattern case file: sig-{number}",
        comment_count=comments,
        updated_at=updated_at,
        area=area,
    )


# --- Age helper -----------------------------------------------------------


def test_proposal_age_hours_whole_hours() -> None:
    now = datetime(2026, 7, 11, 5, 30, tzinfo=UTC)
    assert _proposal_age_hours("2026-07-11T00:00:00+00:00", now) == 5


def test_proposal_age_hours_assumes_utc_for_naive_timestamp() -> None:
    now = datetime(2026, 7, 11, 3, 0, tzinfo=UTC)
    assert _proposal_age_hours("2026-07-11T00:00:00", now) == 3


def test_proposal_age_hours_unparseable_is_zero() -> None:
    assert _proposal_age_hours("not-a-date", datetime.now(UTC)) == 0


# --- View projection ------------------------------------------------------


def test_build_view_sorts_proposals_by_issue_number_with_ages() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    ops = [
        (501, _op(14, created_at="2026-07-11T03:00:00+00:00")),
        (500, _op(13, created_at="2026-07-11T00:00:00+00:00")),
    ]

    view = build_triage_board_view(
        ops=ops, case_files=(), area_counts=(), last_health_review_at=0.0, now=now
    )

    assert [p.proposal_issue_number for p in view.open_proposals] == [500, 501]
    assert view.open_proposals[0].age_hours == 5
    assert view.open_proposals[1].age_hours == 2
    assert view.open_proposals[0].target_issue_number == 13


def test_build_view_ranks_case_files_by_comment_cadence() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    case_files = (
        _summary(700, comments=3, updated_at="2026-07-10T00:00:00+00:00"),
        _summary(701, comments=5, updated_at="2026-07-09T00:00:00+00:00"),
    )

    view = build_triage_board_view(
        ops=(), case_files=case_files, area_counts=(), last_health_review_at=0.0, now=now
    )

    # The higher comment count (the severity signal) ranks first.
    assert [c.issue_number for c in view.case_files] == [701, 700]


def test_build_view_breaks_comment_ties_by_most_recent_update() -> None:
    view = build_triage_board_view(
        ops=(), case_files=(
            _summary(700, comments=3, updated_at="2026-07-09T00:00:00+00:00"),
            _summary(701, comments=3, updated_at="2026-07-10T00:00:00+00:00"),
        ), area_counts=(), last_health_review_at=0.0,
        now=datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )
    assert [case.issue_number for case in view.case_files] == [701, 700]


def test_build_view_formats_last_health_review_from_epoch() -> None:
    now = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    ts = datetime(2026, 7, 11, 0, 0, tzinfo=UTC).timestamp()

    view = build_triage_board_view(
        ops=(), case_files=(), area_counts=(), last_health_review_at=ts, now=now
    )

    assert view.last_health_review == "2026-07-11T00:00:00+00:00"


def test_build_view_last_health_review_empty_when_never() -> None:
    view = build_triage_board_view(
        ops=(), case_files=(), area_counts=(), last_health_review_at=0.0,
        now=datetime.now(UTC),
    )
    assert view.last_health_review == ""


# --- Golden markdown render -----------------------------------------------


POPULATED_VIEW = TriageBoardView(
    open_proposals=(
        TriageBoardProposal(
            proposal_issue_number=500,
            op_type="reset_retry",
            target_issue_number=13,
            age_hours=5,
        ),
    ),
    case_files=(
        TriageBoardCaseFile(
            issue_number=700,
            title="Pattern case file: db-timeout",
            comment_count=3,
            updated_at="2026-07-11T12:00:00+00:00",
            area="db",
        ),
    ),
    area_counts=(("db", 2), ("api", 1)),
    last_health_review="2026-07-11T00:00:00+00:00",
)

POPULATED_GOLDEN = """\
# Triage Board

Orchestrator-authored projection of the triage ledgers (ADR-0031 / #6781).

Last health review: 2026-07-11T00:00:00+00:00

## Open proposals

| Proposal | Operation | Target | Age |
|---|---|---|---|
| #500 | `reset_retry` | #13 | 5h |

## Open pattern case files

| Case file | Title | Comments | Updated | Area |
|---|---|---|---|---|
| #700 | Pattern case file: db-timeout | 3 | 2026-07-11T12:00:00+00:00 | db |

## Case files by area

- db: 2
- api: 1
"""

EMPTY_GOLDEN = """\
# Triage Board

Orchestrator-authored projection of the triage ledgers (ADR-0031 / #6781).

Last health review: never

## Open proposals

None.

## Open pattern case files

None.

## Case files by area

None.
"""


def test_render_populated_board_is_golden() -> None:
    assert render_triage_board_md(POPULATED_VIEW) == POPULATED_GOLDEN


def test_render_empty_board_is_golden() -> None:
    empty = TriageBoardView(
        open_proposals=(), case_files=(), area_counts=(), last_health_review=""
    )
    assert render_triage_board_md(empty) == EMPTY_GOLDEN


def test_render_is_deterministic() -> None:
    assert render_triage_board_md(POPULATED_VIEW) == render_triage_board_md(POPULATED_VIEW)


def test_render_escapes_table_breaking_issue_text() -> None:
    view = TriageBoardView(
        open_proposals=(),
        case_files=(TriageBoardCaseFile(
            issue_number=700, title="Pattern | case\nfile", comment_count=1,
            updated_at="", area="db|storage",
        ),),
        area_counts=(("db|storage", 1),), last_health_review="",
    )
    rendered = render_triage_board_md(view)
    assert "Pattern \\| case file" in rendered
    assert "db\\|storage" in rendered


# --- Publisher -------------------------------------------------------------


def _publisher(tmp_path: Path, authority=None) -> TriageBoardPublisher:
    return TriageBoardPublisher(
        board_path=triage_board_path(tmp_path),
        authority=authority if authority is not None else InMemoryTriageAuthorityStore(),
        clock=lambda: datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )


def test_board_path_lives_in_state_dir(tmp_path: Path) -> None:
    assert triage_board_path(tmp_path) == state_dir(tmp_path) / TRIAGE_BOARD_FILENAME


def test_publish_retains_case_files_and_writes_board(tmp_path: Path) -> None:
    facts = TriageFacts(
        open_case_files=(
            _summary(700, comments=3, updated_at="2026-07-11T12:00:00+00:00", area="db"),
        ),
        case_files_scanned=True,
    )
    publisher = _publisher(tmp_path)
    publisher.publish(facts, last_health_review_at=0.0)
    assert publisher.case_files() == facts.open_case_files
    board = triage_board_path(tmp_path)
    assert board.exists()
    content = board.read_text()
    assert content.startswith("# Triage Board")
    assert "#700" in content
    assert "Pattern case file: sig-700" in content


def test_publisher_reads_shipped_fixes_from_durable_authority(tmp_path: Path) -> None:
    authority = InMemoryTriageAuthorityStore()
    authority.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )
    publisher = _publisher(tmp_path, authority)

    [fix] = publisher.shipped_fixes(10)

    assert (fix.issue_number, fix.area) == (600, "db")


def test_publish_throttles_unchanged_content(tmp_path: Path) -> None:
    """Identical facts render identically -> no second write (#6781)."""
    facts = TriageFacts(
        open_case_files=(_summary(700, comments=1),), case_files_scanned=True
    )
    publisher = _publisher(tmp_path)
    publisher.publish(facts, last_health_review_at=0.0)
    board = triage_board_path(tmp_path)
    assert board.exists()
    board.unlink()  # remove the artifact

    publisher.publish(facts, last_health_review_at=0.0)

    assert not board.exists()  # never rewritten


def test_publish_rewrites_when_content_changes(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(
        TriageFacts(open_case_files=(_summary(700, comments=1),), case_files_scanned=True),
        last_health_review_at=0.0,
    )
    publisher.publish(
        TriageFacts(open_case_files=(_summary(700, comments=2),), case_files_scanned=True),
        last_health_review_at=0.0,
    )

    content = triage_board_path(tmp_path).read_text()
    assert "| 2 |" in content  # the newer comment count landed


def test_publish_swallows_render_failure(tmp_path: Path) -> None:
    """A projection write must never fail the planning tick (#6781)."""
    def boom() -> datetime:
        raise RuntimeError("clock exploded")

    publisher = TriageBoardPublisher(
        board_path=triage_board_path(tmp_path),
        authority=InMemoryTriageAuthorityStore(),
        clock=boom,
    )
    facts = TriageFacts(open_case_files=(_summary(700),), case_files_scanned=True)
    publisher.publish(facts, last_health_review_at=0.0)  # must not raise
    assert publisher.case_files() == facts.open_case_files
    assert not triage_board_path(tmp_path).exists()


def test_publish_swallows_write_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    publisher = TriageBoardPublisher(
        board_path=blocker / "triage-board.md",
        authority=InMemoryTriageAuthorityStore(),
        clock=lambda: datetime(2026, 7, 11, 5, 0, tzinfo=UTC),
    )

    publisher.publish(
        TriageFacts(open_case_files=(_summary(700),), case_files_scanned=True),
        last_health_review_at=0.0,
    )
    # No exception propagated.


# --- Retain-vs-clear across scanned / not-scanned ticks (#6781 R2) ---------


def test_publish_retains_prior_case_files_when_scan_did_not_run(tmp_path: Path) -> None:
    """A frugal tick (no anchor scan) must NOT wipe the durable projection.

    Regression for #6781 R2: a health-review-armed but not-due tick gathers
    facts with ``case_files_scanned=False`` and ``open_case_files=()``. That
    empty tuple means "not observed this tick", not "observed empty" — the
    publisher must retain the last scanned projection so the board snapshot
    keeps seeing accumulating case-file evidence between scans.
    """
    publisher = _publisher(tmp_path)
    scanned = TriageFacts(
        open_case_files=(
            _summary(700, comments=3, updated_at="2026-07-11T12:00:00+00:00", area="db"),
        ),
        case_files_scanned=True,
    )
    publisher.publish(scanned, last_health_review_at=0.0)
    assert publisher.case_files() == scanned.open_case_files

    # A subsequent no-scan tick: empty case files, scanned flag off.
    not_scanned = TriageFacts(open_case_files=(), case_files_scanned=False)
    publisher.publish(not_scanned, last_health_review_at=0.0)

    # The injected reader (what the board snapshot builder consumes) still
    # holds the prior case file...
    assert publisher.case_files() == scanned.open_case_files
    # ...and the rendered board still surfaces it rather than "None".
    content = triage_board_path(tmp_path).read_text()
    assert "#700" in content
    assert "Pattern case file: sig-700" in content


def test_publish_clears_case_files_when_scan_observed_none(tmp_path: Path) -> None:
    """A real scan that observed no open case files DOES clear the projection.

    The counterpart to the retain-on-no-scan case: when the anchor scan runs
    (``case_files_scanned=True``) and finds nothing, the empty tuple is a
    genuine observation, so stale case files are removed from both the reader
    and the board.
    """
    publisher = _publisher(tmp_path)
    publisher.publish(
        TriageFacts(
            open_case_files=(_summary(700, comments=3, area="db"),),
            case_files_scanned=True,
        ),
        last_health_review_at=0.0,
    )
    assert publisher.case_files()  # sanity: something to clear

    # A real scan observing an empty ledger.
    publisher.publish(
        TriageFacts(open_case_files=(), case_files_scanned=True),
        last_health_review_at=0.0,
    )

    assert publisher.case_files() == ()
    content = triage_board_path(tmp_path).read_text()
    assert "#700" not in content
    # The case-file section collapses to the empty marker.
    assert "## Open pattern case files\n\nNone." in content
