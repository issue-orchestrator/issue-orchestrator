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
    * The fact never moves backward. If the recorded head already contains
      the PR head, nothing is written.
    * The fact may move to a head that diverges from the recorded one. That
      is the completion's force-push of a rebased rework, and the PR head is
      now what the branch publishes.
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
        recorded = replace(key, validated_head_sha=fact.published_head_sha)
        relation = lineage.compare(CommitReference(head, ""), CommitReference(recorded, ""))
        if relation in {Relation.EQUAL, Relation.ANCESTOR}:
            return Status.ALREADY_PUBLISHED
        if relation not in {Relation.DESCENDANT, Relation.DIVERGENT}:
            return Status.CONTAINMENT_UNPROVEN
    conn.execute(
        "INSERT INTO validated_work_lineage VALUES (?,?,?,?,?,?) ON CONFLICT(lineage_key) DO UPDATE SET "
        "published_head_sha=excluded.published_head_sha,published_by_record_id=excluded.published_by_record_id,"
        "published_via=excluded.published_via,published_pre_push_expected=excluded.published_pre_push_expected,"
        "published_at=excluded.published_at",
        (
            lineage_key,
            published.head_sha,
            canonical_record_id(key),
            PublicationProvenance.OBSERVED_OPEN_PR.value,
            "",
            observed_at,
        ),
    )
    lineage.classify(conn, lineage_key, observed_at)
    return Status.ADVANCED
