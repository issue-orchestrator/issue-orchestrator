"""Record the lineage head a PR publishes, then reclassify the lineage.

Two verified publication routes that are not recovery's own push (§2.1.4,
§2.7): the completion's push observed on the issue's open PR, and a merged PR
of the branch (§3.5), proven by its head at merge - a squash merge leaves
none of the branch's commits on the base. The store records that head as the
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
from ..domain.validated_work_remote_authority import PullRequestPublication
from ..domain.validated_work_store import (
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
            published.provenance.value,
            "",
            observed_at,
            published.pr_number,
        ),
    )
    # Every record this resolves is stamped with the PR by the classifier
    # (``published_pr_number``), as is any later admission contained in it.
    lineage.classify(conn, lineage_key, observed_at)
    return Status.ADVANCED
