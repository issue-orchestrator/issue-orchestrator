"""Record the lineage head a PR publishes, then reclassify the lineage.

Three verified publication routes that are not recovery's own push (§2.1.4,
§2.7): the completion's push observed on the issue's open PR, a merged PR
of the branch (§3.5), proven by its head at merge - a squash merge leaves
none of the branch's commits on the base - and an open or merged PR of the
same issue on ANOTHER branch, where the work was republished (#8137). The store records that head as the
lineage's published head only after it has itself verified that a validated
head of the lineage is contained in it. Reclassifying against the new fact is
what resolves the contained records: ``CONTAINED_IN_PUBLISHED_HEAD`` for an
open PR, ``LANDED_VIA_MERGED_PR`` for a merged one. Before these routes
existed, recovery parked such records ``divergent_validated_heads`` forever
(porchpin #186, #26, #320).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from ..domain.validated_work import (
    PublicationProvenance,
    ValidatedWorkKey,
    canonical_lineage_key,
    canonical_record_id,
)
from ..domain.validated_work_remote_authority import (
    CarriedByIssuePullRequest,
    LandedViaMergedPullRequest,
    PullRequestPublication,
)
from ..domain.validated_work_store import (
    carrier_ref,
    landing_ref,
    AncestryRelation as Relation,
    CommitReference,
    PrPublicationStatus as Status,
)
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import publication


def record_pr_publication(
    conn: sqlite3.Connection,
    lineage: LineageClassifier,
    *,
    key: ValidatedWorkKey,
    published: PullRequestPublication,
    observed_at: str,
) -> Status:
    """Advance the lineage fact to a PR's head when it carries ``key``'s head.

    The PR is the issue's open PR (``OBSERVED_OPEN_PR``), or a merged PR of the
    branch, whose head at merge is the published head even when a squash
    merge left none of its commits on the base (``OBSERVED_MERGE``).

    Rules, all inside the caller's write transaction:

    * The store verifies the containment itself: ``key``'s validated head
      must equal the PR head or be one of its ancestors. It does not trust the
      caller's proof.
    * A lineage with a PUBLISHING record is left alone. That in-flight
      publication owns the remote expectation (§4.4e).
    * The fact follows the PR: whatever the PR head's relation to the
      recorded head (descendant, divergent after a rebased rework's
      force-push, or even an ancestor after a force-push back), the PR head is
      what the branch publishes now, and the store has just proven it carries
      validated work of this lineage. Only the head recovery itself pushed, or
      this PR's already recorded publication of the same head, is left as is.
    """
    lineage_key = canonical_lineage_key(key)
    head = replace(key, validated_head_sha=published.head_sha)
    if lineage.compare(CommitReference(key, ""), CommitReference(head, "")) not in {
        Relation.EQUAL,
        Relation.ANCESTOR,
    }:
        return Status.CONTAINMENT_UNPROVEN
    if conn.execute(
        "SELECT 1 FROM validated_work_records WHERE lineage_key=? AND state='publishing'",
        (lineage_key,),
    ).fetchone():
        return Status.PUBLICATION_IN_FLIGHT
    if isinstance(published, LandedViaMergedPullRequest):
        return _record_landing(conn, lineage, key, published, observed_at)
    if isinstance(published, CarriedByIssuePullRequest):
        return _record_carrier(conn, lineage, key, published, observed_at)
    fact = publication(conn, lineage_key)
    if fact is not None:
        if fact.published_head_sha == published.head_sha and (
            fact.published_via is PublicationProvenance.PUSHED_BY_OWNER
            or fact.published_pr_number == published.pr_number
        ):
            # Recovery pushed this very head (and routed it), or this PR's
            # publication of it is already recorded.
            return Status.ALREADY_PUBLISHED
        recorded = replace(key, validated_head_sha=fact.published_head_sha)
        if lineage.compare(CommitReference(head, ""), CommitReference(recorded, "")) not in {
            Relation.EQUAL, Relation.ANCESTOR, Relation.DESCENDANT, Relation.DIVERGENT,
        }:
            return Status.CONTAINMENT_UNPROVEN
    conn.execute(
        "INSERT INTO validated_work_lineage (lineage_key,published_head_sha,published_by_record_id,"
        "published_via,published_pre_push_expected,published_at,published_pr_number) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(lineage_key) DO UPDATE SET "
        "published_head_sha=excluded.published_head_sha,published_by_record_id=excluded.published_by_record_id,"
        "published_via=excluded.published_via,published_pre_push_expected=excluded.published_pre_push_expected,"
        "published_at=excluded.published_at,published_pr_number=excluded.published_pr_number",
        (
            lineage_key,
            published.head_sha,
            canonical_record_id(key),
            PublicationProvenance.OBSERVED_OPEN_PR.value,
            "",
            observed_at,
            published.pr_number,
        ),
    )
    # Every record this resolves is stamped with the PR by the classifier
    # (``published_pr_number``), as is any later admission contained in it.
    lineage.classify(conn, lineage_key, observed_at)
    return Status.ADVANCED


def _record_landing(
    conn: sqlite3.Connection,
    lineage: LineageClassifier,
    key: ValidatedWorkKey,
    landed: LandedViaMergedPullRequest,
    observed_at: str,
) -> Status:
    """Record a merged PR's head at merge as a landing, then reclassify.

    A landing never touches the lineage fact: the fact is what the branch
    publishes now, and on a reused branch that can be a newer, divergent open
    PR whose baseline recovery still sequences from. A merged PR's head is
    immutable, so a second proof of the same PR is already recorded - even
    after the same head was first recorded as that PR's open publication,
    the landing is new and resolves its ancestors as landed.
    """
    lineage_key = canonical_lineage_key(key)
    recorded = conn.execute(
        "SELECT head_sha FROM validated_work_lineage_landings WHERE lineage_key=? AND pr_number=?",
        (lineage_key, landed.pr_number),
    ).fetchone()
    if recorded is not None:
        if recorded["head_sha"] != landed.head_sha:
            return Status.CONTAINMENT_UNPROVEN  # a merged PR's head cannot move
        return Status.ALREADY_PUBLISHED
    # Pin the head at merge before the row commits: once the fetched tracking
    # ref is pruned, nothing else keeps a squash-merged head reachable.
    pin = CommitReference(replace(key, validated_head_sha=landed.head_sha), landing_ref(lineage_key, landed.pr_number))
    if not lineage.retain(pin):
        return Status.CONTAINMENT_UNPROVEN
    conn.execute(
        "INSERT INTO validated_work_lineage_landings (lineage_key,pr_number,head_sha,landed_at) VALUES (?,?,?,?)",
        (lineage_key, landed.pr_number, landed.head_sha, observed_at),
    )
    lineage.classify(conn, lineage_key, observed_at)
    return Status.ADVANCED


def _record_carrier(
    conn: sqlite3.Connection,
    lineage: LineageClassifier,
    key: ValidatedWorkKey,
    carried: CarriedByIssuePullRequest,
    observed_at: str,
) -> Status:
    """Record another branch's PR of the issue as carrying the lineage's work (#8137).

    Never the lineage fact: that is what THIS branch publishes, and recovery
    sequences from it. A merged PR's head cannot move, so a second proof of
    it is already recorded, and its row resolves every record it contains
    from then on. An open PR can be force-pushed or closed after any proof,
    so its row resolves records only at the instant that proved it current
    (review r1): every proof of an open PR re-stamps the row and reclassifies
    - ADVANCED - even when its head did not move.
    """
    lineage_key = canonical_lineage_key(key)
    recorded = conn.execute(
        "SELECT head_sha, merged FROM validated_work_lineage_carriers WHERE lineage_key=? AND pr_number=?",
        (lineage_key, carried.pr_number),
    ).fetchone()
    if recorded is not None and recorded["merged"]:
        if recorded["head_sha"] != carried.head_sha or not carried.merged:
            return Status.CONTAINMENT_UNPROVEN  # a merged PR's head cannot move
        return Status.ALREADY_PUBLISHED
    # Pin before the row commits: once the fetched PR ref is pruned, nothing
    # else keeps a rebased-away or squash-merged head reachable.
    pin = CommitReference(
        replace(key, validated_head_sha=carried.head_sha),
        carrier_ref(lineage_key, carried.pr_number, carried.head_sha),
    )
    if not lineage.retain(pin):
        return Status.CONTAINMENT_UNPROVEN
    conn.execute(
        "INSERT INTO validated_work_lineage_carriers "
        "(lineage_key,pr_number,branch_name,head_sha,merged,observed_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(lineage_key,pr_number) DO UPDATE SET branch_name=excluded.branch_name,"
        "head_sha=excluded.head_sha,merged=excluded.merged,observed_at=excluded.observed_at",
        (lineage_key, carried.pr_number, carried.branch_name, carried.head_sha,
         int(carried.merged), observed_at),
    )
    lineage.classify(conn, lineage_key, observed_at)
    return Status.ADVANCED
