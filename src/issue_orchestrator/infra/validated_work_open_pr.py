"""Record the lineage head an open PR publishes, then reclassify the lineage.

The third verified publication route (§2.1.4): the issue's completion pushed
its own validated work into its open PR. The store records that head as the
lineage's published head, with provenance ``OBSERVED_OPEN_PR``, only after it
has itself verified that a validated head of the lineage is contained in it.
Reclassifying against the new fact is what resolves contained records
``RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)``. Before this route existed,
recovery compared a rework's rebased heads with the lineage's older published
head and parked them as divergent (porchpin #186).
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
from ..domain.validated_work_remote_authority import PublishedOnOpenPullRequest
from ..domain.validated_work_store import (
    AncestryRelation as Relation,
    CommitReference,
    OpenPrPublicationStatus as Status,
)
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import publication


def record_open_pr_publication(
    conn: sqlite3.Connection,
    lineage: LineageClassifier,
    *,
    key: ValidatedWorkKey,
    published: PublishedOnOpenPullRequest,
    observed_at: str,
) -> Status:
    """Advance the lineage fact to the open PR's head when it carries ``key``'s head.

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
