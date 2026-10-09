"""Work republished on another branch's PR of its issue is published (#8137).

porchpin#262, 2026-10-04: a review exchange validated slice 2 on the issue's
branch, whose PR (#457) had already merged. The completion's PR collided, so it
moved to ``<branch>-r1`` and opened PR #479 there. Every validated head was
captured under the ORIGINAL branch, and both the proof route and published-
review custody looked for a PR only on that branch. Three records stayed
parked ``divergent_validated_heads`` for days, ``recovery-pending`` kept coming
back, and the stuck sweep read the issue as abandoned.

The right answer is content, not the branch name: a record whose validated
head an open or merged PR of the same issue contains is published, wherever
that PR's branch is. The same real-Git rig as porchpin #186's
(``test_validated_work_published_rework``): a bare ``origin``, SQLite, intake,
escrow and the aggregate recovery block; only GitHub's PR listing is a fake,
and it reads the real bare remote.
"""

from pathlib import Path

import pytest

from issue_orchestrator.control.published_review_custody import (
    PublishedReviewCustody, PublishedValidatedWorkHeld,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.publication_remote import PublicationPrState, PublicationPullRequest
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.validated_work import (
    PublicationProvenance, ResolutionKind, ValidatedWorkFailure, ValidatedWorkKey, ValidatedWorkState,
    canonical_lineage_key,
)
from issue_orchestrator.domain.validated_work_remote_authority import (
    CarriedByIssuePullRequest, carried_by_issue_pull_request,
)
from issue_orchestrator.domain.validated_work_store import AncestryRelation, PrPublicationStatus
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.unit.test_validated_work_published_rework import (
    BRANCH, ISSUE, PR, RECOVERY_PENDING, REPO, _commit, _drain, _fact, _legacy_parked_records, _run, _sweep,
    _validate, build_rig,
)

REPUBLISHED = f"{BRANCH}-r1"
ELSEWHERE = 479
HEAD = "a" * 40
OTHER = "b" * 40


@pytest.fixture
def rig(tmp_path):
    return build_rig(tmp_path)


def _republish(rig, *, on_top: bool = True) -> str:
    """The completion's collision remediation: the branch's own PR is gone, and
    the validated work is pushed to ``-r1`` where PR #479 opens. ``on_top``
    adds a later commit there, as the slice's next session did."""
    rig.github.open = False  # the original branch has no open PR any more
    if on_top:
        _commit(rig.git, rig.worktree, "next-slice", "a later commit on the republished branch")
    head = rig.git.head_sha(rig.worktree)
    rig.git.run(rig.worktree, ["push", "-q", "origin", f"HEAD:refs/heads/{REPUBLISHED}"])
    rig.git.run(rig.origin, ["update-ref", f"refs/pull/{ELSEWHERE}/head", head])
    rig.github.elsewhere[ELSEWHERE] = (REPUBLISHED, PublicationPrState.OPEN)
    return head


class _OpenPulls:
    """GitHub's uncached open-PR read by branch, for published-review custody."""

    def __init__(self, rig) -> None:
        self._rig = rig
        self.reads: list[str] = []

    def get_open_prs_for_branch_complete(self, branch: str) -> list[PRInfo]:
        self.reads.append(branch)
        return [
            PRInfo(number=number, title=f"#{ISSUE}", url="", branch=pr_branch, body="", state="open", labels=[],
                   head_sha=self._rig.github._pull_head(number))
            for number, (pr_branch, state) in self._rig.github.elsewhere.items()
            if pr_branch == branch and state is PublicationPrState.OPEN
        ]


def _resolved_elsewhere(rig, parked, *, head: str, kind: ResolutionKind) -> None:
    for disposition in parked:
        record = rig.store.record_for_id(disposition.record_id)
        assert record.disposition.state is ValidatedWorkState.RECOVERED, record.disposition
        assert record.resolution_kind is kind
        assert record.disposition.published_head_sha == head
        assert record.disposition.pr_number == ELSEWHERE
        # Custody reads the PR on the branch that actually publishes the work.
        assert record.disposition.publication_branch == REPUBLISHED
        assert record.disposition.key.branch_name == BRANCH  # the record keeps its own branch
        # No validated commit is released.
        pinned = record.current_evidence.admission.pinned_ref
        assert rig.git.run(rig.repo, ["rev-parse", pinned]).stdout.strip() == disposition.key.validated_head_sha


def test_parked_records_another_branchs_open_pr_carries_resolve_and_release_the_issue(
    rig, make_session, monkeypatch,
):
    """porchpin#262: the drain's scope sweep finds the issue's PR on ``-r1``,
    the records resolve as contained in its head, ``recovery-pending`` comes
    off, and published-review custody holds the issue for that PR."""
    w1, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    head = _republish(rig)

    report = _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    assert report.scope_sweep.retired == ()
    assert report.scope_sweep.published
    assert not rig.store.has_unresolved_work(ISSUE)
    _resolved_elsewhere(rig, parked, head=head, kind=ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD)
    # The carrier sits beside the branch's own fact; it never replaces it.
    assert _fact(rig).published_head_sha == w1
    assert RECOVERY_PENDING not in rig.labels.labels
    # The stuck sweep and reset_retry ask this owner (#7293): PR #479 holds the issue.
    pulls = _OpenPulls(rig)
    custody = PublishedReviewCustody(rig.store, pulls)
    assert [(hold.pr_number, hold.branch_name) for hold in custody.holds(ISSUE)] == [(ELSEWHERE, REPUBLISHED)]
    # W1, which recovery published on the branch itself, is read there.
    assert pulls.reads == sorted([BRANCH, REPUBLISHED])
    with pytest.raises(PublishedValidatedWorkHeld):
        custody.require_released(ISSUE)


def test_parked_records_another_branchs_merged_pr_landed_resolve(rig, make_session, monkeypatch):
    """The same PR after it squash-merged: its head at merge lands the work,
    although neither its branch nor the base contains the record's commits."""
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    head = _republish(rig)
    rig.git.run(rig.origin, ["update-ref", "-d", f"refs/heads/{REPUBLISHED}"])
    rig.github.elsewhere[ELSEWHERE] = (REPUBLISHED, PublicationPrState.MERGED)

    _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    assert not rig.store.has_unresolved_work(ISSUE)
    _resolved_elsewhere(rig, parked, head=head, kind=ResolutionKind.LANDED_VIA_MERGED_PR)
    assert RECOVERY_PENDING not in rig.labels.labels
    # A merged PR holds nothing open.
    assert PublishedReviewCustody(rig.store, _OpenPulls(rig)).holds(ISSUE) == ()


@pytest.mark.parametrize("doubt", ["rebased-away", "closed-unmerged", "fork", "unreadable"])
def test_a_pr_elsewhere_not_proven_to_carry_the_work_leaves_it_parked(rig, make_session, monkeypatch, doubt):
    """porchpin#262 as it stands now: PR #479 was later rebased with conflict
    resolution, so its head no longer contains the parked heads. Content is
    the proof, so they stay held - as they do for a PR closed unmerged, a
    fork's PR, or an unreadable GitHub."""
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    _republish(rig)
    if doubt == "rebased-away":
        rig.git.run(rig.worktree, ["checkout", "-q", "--detach", "main"])
        _commit(rig.git, rig.worktree, "rebuilt", "the slice rebuilt with a conflict resolved")
        rig.git.run(rig.worktree, ["push", "-q", "--force", "origin", f"HEAD:refs/pull/{ELSEWHERE}/head"])
    elif doubt == "closed-unmerged":
        rig.github.elsewhere[ELSEWHERE] = (REPUBLISHED, PublicationPrState.CLOSED)
    elif doubt == "fork":
        rig.github.head_repo = "fork/repo"
    else:
        rig.github.unreadable = True

    _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    for disposition in parked:
        record = rig.store.get(disposition.record_id)
        assert (record.state, record.failure) == (
            ValidatedWorkState.PARKED, ValidatedWorkFailure.DIVERGENT_VALIDATED_HEADS)
    assert RECOVERY_PENDING in rig.labels.labels


def test_a_capture_whose_pr_collided_onto_another_branch_is_published_not_parked(rig):
    """The capture route, the moment porchpin#262's records were made: the
    completion's PR collided onto ``-r1``. The capture records PR #479's
    carriage before it admits, so the record never parks and the issue is
    never blocked."""
    coding = _run(rig, SessionKind.CODE, "coding-1", f"issue-{ISSUE}")
    _commit(rig.git, rig.worktree, "journey", "slice two")
    validated = _validate(rig, coding, "coding-1")
    head = _republish(rig, on_top=False)
    assert head == validated

    assert rig.lifecycle.preserve_completed_run(
        ISSUE, f"issue-{ISSUE}", "session-completion", run=coding) is False

    (record,) = rig.store.for_issue(ISSUE).dispositions
    assert (record.state, record.pr_number, record.publication_branch) == (
        ValidatedWorkState.RECOVERED, ELSEWHERE, REPUBLISHED)
    assert rig.store.record_for_id(record.record_id).resolution_kind is ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD
    assert rig.labels.operations == []


def test_the_branchs_own_pr_is_never_read_as_another_branchs(rig, make_session, monkeypatch):
    """A PR on the record's own branch is judged by the stricter same-branch
    routes only. Listed among the issue's PRs, it proves nothing new."""
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    rig.github.open = False  # the own-branch open route proves nothing ...
    head = rig.git.head_sha(rig.worktree)
    rig.git.run(rig.origin, ["update-ref", f"refs/pull/{PR}/head", head])
    rig.github.elsewhere[PR] = (BRANCH, PublicationPrState.OPEN)  # ... and the issue read lists it

    _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)

    for disposition in parked:
        assert rig.store.get(disposition.record_id).state is ValidatedWorkState.PARKED


# -- the proof (domain) --------------------------------------------------------


def _pull(*, state=PublicationPrState.OPEN, branch=REPUBLISHED, head_repo=REPO, base_repo=REPO, head=HEAD):
    return PublicationPullRequest(ELSEWHERE, f"https://github.com/{REPO}/pull/{ELSEWHERE}", head_repo, base_repo,
                                  branch, "main", head, state, f"Refs #{ISSUE}")


@pytest.mark.parametrize(("pr", "fetched", "relation"), [
    (_pull(state=PublicationPrState.CLOSED), HEAD, AncestryRelation.EQUAL),
    (_pull(branch=BRANCH), HEAD, AncestryRelation.EQUAL),
    (_pull(head_repo="fork/repo"), HEAD, AncestryRelation.EQUAL),
    (_pull(base_repo="fork/repo"), HEAD, AncestryRelation.EQUAL),
    (_pull(), OTHER, AncestryRelation.EQUAL),
    (_pull(), None, None),
    (_pull(), HEAD, AncestryRelation.DESCENDANT),
    (_pull(), HEAD, AncestryRelation.DIVERGENT),
    (_pull(), HEAD, AncestryRelation.RIGHT_UNREACHABLE),
], ids=["closed-unmerged", "own-branch", "fork-head", "fork-base", "pull-ref-moved", "pull-ref-unfetchable",
        "head-ahead-of-pr", "divergent", "unreachable"])
def test_only_every_fact_agreeing_proves_another_branchs_carriage(pr, fetched, relation):
    assert carried_by_issue_pull_request(
        pr, repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=fetched, relation=relation) is None


@pytest.mark.parametrize("state", [PublicationPrState.OPEN, PublicationPrState.MERGED])
@pytest.mark.parametrize("relation", [AncestryRelation.EQUAL, AncestryRelation.ANCESTOR])
def test_an_open_or_merged_pr_elsewhere_whose_head_carries_the_work_proves_it(state, relation):
    proof = carried_by_issue_pull_request(
        _pull(state=state), repo_slug=REPO, branch_name=BRANCH, fetched_head_sha=HEAD, relation=relation)
    merged = state is PublicationPrState.MERGED
    assert proof == CarriedByIssuePullRequest(ELSEWHERE, HEAD, REPUBLISHED, merged=merged)
    assert proof.provenance is (
        PublicationProvenance.OBSERVED_MERGE if merged else PublicationProvenance.OBSERVED_OPEN_PR)
    assert f"PR #{ELSEWHERE} ({REPUBLISHED})" in proof.describe(OTHER)


# -- the store -----------------------------------------------------------------


def test_a_carrier_follows_an_open_pr_and_freezes_when_it_merges(tmp_path):
    """An open PR's carriage follows its head; a merged one cannot move, and
    the same proof again is already recorded."""
    from tests.unit.validated_work_support import L, ROOT, TIP, Rig, capture

    store = Rig(tmp_path / "work.sqlite").open()
    key = capture(L).evidence.identity.key
    open_at_l = CarriedByIssuePullRequest(91, L, "feature-r1", merged=False)

    assert store.record_pr_publication(key, published=open_at_l, observed_at="2026-10-04T09:00:00+00:00") \
        is PrPublicationStatus.ADVANCED
    assert store.record_pr_publication(key, published=open_at_l, observed_at="2026-10-04T09:15:00+00:00") \
        is PrPublicationStatus.ALREADY_PUBLISHED
    assert store.record_pr_publication(
        key, published=CarriedByIssuePullRequest(91, TIP, "feature-r1", merged=False),
        observed_at="2026-10-04T10:00:00+00:00") is PrPublicationStatus.ADVANCED
    assert store.record_pr_publication(
        key, published=CarriedByIssuePullRequest(91, TIP, "feature-r1", merged=True),
        observed_at="2026-10-04T11:00:00+00:00") is PrPublicationStatus.ADVANCED
    assert store.record_pr_publication(
        key, published=CarriedByIssuePullRequest(91, L, "feature-r1", merged=True),
        observed_at="2026-10-04T12:00:00+00:00") is PrPublicationStatus.CONTAINMENT_UNPROVEN
    # The lineage's own fact is untouched: the carrier is another branch's PR.
    assert store.lineage_publication(canonical_lineage_key(key)) is None
    # A capture the merged carrier contains is landed at admission.
    late = capture(L, run="run-late", expected=ROOT)
    store.admit(late)
    record = store.record_for_id(late.evidence.record_id)
    assert (record.disposition.state, record.resolution_kind, record.disposition.publication_branch) == (
        ValidatedWorkState.RECOVERED, ResolutionKind.LANDED_VIA_MERGED_PR, "feature-r1")


def test_an_existing_store_gains_the_carriers_table_and_published_branch_on_open(tmp_path):
    import sqlite3

    from tests.unit.validated_work_support import Rig

    path = tmp_path / "work.sqlite"
    Rig(path).open()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE validated_work_lineage_carriers")
        conn.execute("ALTER TABLE validated_work_records DROP COLUMN published_branch")

    Rig(path).open()

    with sqlite3.connect(path) as conn:
        assert "published_branch" in {row[1] for row in conn.execute("PRAGMA table_info(validated_work_records)")}
        assert conn.execute("SELECT count(*) FROM validated_work_lineage_carriers").fetchone() == (0,)


def test_a_carriers_pin_is_permanent(rig):
    """The carried head is pinned under a ref escrow reconciliation never
    enumerates and nothing may release."""
    from issue_orchestrator.execution.git_exact_operations import GitExactOperations
    from issue_orchestrator.domain.validated_work_store import carrier_ref

    head = rig.git.head_sha(rig.repo)
    ref = carrier_ref(canonical_lineage_key(ValidatedWorkKey(REPO, ISSUE, BRANCH, head)), ELSEWHERE, head)
    ops = GitExactOperations(rig.git, None)
    ops.pin_ref(Path(rig.repo), ref=ref, sha=head)

    assert ops.verify_ref(Path(rig.repo), ref=ref, sha=head)
    assert ref not in {pin.ref for pin in ops.retained_refs(Path(rig.repo))}
    with pytest.raises(ValueError, match="only escrow"):
        ops.delete_pinned_ref(Path(rig.repo), ref=ref, sha=head)
