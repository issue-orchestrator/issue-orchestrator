"""The four durable lifetimes of validated-work disposition."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS validated_work_records (
    record_id             TEXT PRIMARY KEY,           -- canonical ValidatedWorkKey
    repo_slug             TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    branch_name           TEXT NOT NULL,
    validated_head_sha    TEXT NOT NULL,
    lineage_key           TEXT NOT NULL,              -- canonical (repo, issue, branch) §2.1.4
    lineage_role          TEXT NOT NULL,              -- LineageRole
    superseded_by_record_id TEXT NOT NULL DEFAULT '', -- the HEAD this ancestor waits on
    waits_on_record_id    TEXT NOT NULL DEFAULT '',   -- §2.1.4 PENDING successor
    owner_fence           INTEGER NOT NULL DEFAULT 0, -- §4.4e; monotonic, never reused
    owner_host            TEXT NOT NULL DEFAULT '',   -- current claim holder
    owner_pid             INTEGER NOT NULL DEFAULT 0,
    owner_started_at      TEXT NOT NULL DEFAULT '',   -- pid reuse guard
    owner_claim_hash      TEXT NOT NULL DEFAULT '',   -- sha256 of the claim secret
    owner_instance_id     TEXT NOT NULL DEFAULT '',   -- repo-lock instance, for the death proof
    owner_claimed_at      TEXT NOT NULL DEFAULT '',   -- diagnostics/UI only, never authority
    stop_reserved_fence   INTEGER NOT NULL DEFAULT -1, -- §8.4; -1 = no reservation
    stop_reserved_engine  TEXT NOT NULL DEFAULT '',    -- the engine the stop targets
    stop_reservation_id   TEXT NOT NULL DEFAULT '',    -- unique, never shared by callers
    stop_reserved_at      TEXT NOT NULL DEFAULT '',
    state                 TEXT NOT NULL,              -- ValidatedWorkState
    failure               TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure
    reason                TEXT NOT NULL DEFAULT '',
    finalization_phase    TEXT NOT NULL DEFAULT 'not_started',  -- FinalizationPhase (§4.5)
    publishing_started_at TEXT NOT NULL DEFAULT '',   -- set by the first attempt claim
    published_head_sha    TEXT NOT NULL DEFAULT '',
    resolution_kind       TEXT NOT NULL DEFAULT '',   -- ResolutionKind
    resolved_by           TEXT NOT NULL DEFAULT '',   -- OperatorResolution.actor
    resolution_reason     TEXT NOT NULL DEFAULT '',
    resolved_at           TEXT NOT NULL DEFAULT '',
    abandon_authority_json TEXT NOT NULL DEFAULT '', -- most recent accepted abandonment snapshot
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    terminal_at           TEXT NOT NULL DEFAULT ''    -- entry into a RESOLVED state
);

-- One row per admitted evidence, keyed by the id operators and ops name.
CREATE TABLE IF NOT EXISTS validated_work_evidence (
    evidence_id           TEXT PRIMARY KEY,
    record_id             TEXT NOT NULL
                          REFERENCES validated_work_records(record_id),
    role                  TEXT NOT NULL,              -- EvidenceRole
    identity              TEXT NOT NULL,              -- ValidatedWorkIdentity JSON
    observations          TEXT NOT NULL,              -- mutable half of the evidence
    worktree_head_sha     TEXT NOT NULL,              -- observed at capture (§1.1)
    expected_remote_head  TEXT NOT NULL DEFAULT '',   -- '' = no remote branch expected
    pr_number             INTEGER,
    escrow_dir            TEXT NOT NULL,              -- relative to escrow root
    pinned_ref            TEXT NOT NULL,              -- validated commit
    observed_ref          TEXT NOT NULL DEFAULT '',   -- unvalidated worktree head, if any
    observation_revision  INTEGER NOT NULL DEFAULT 0, -- §2.1.1; bumped by every refresh
    -- The disposition THIS evidence was admitted with (§5). Durable because an
    -- attached row may be promoted long after capture, and promoting it as QUEUED
    -- when it was captured PARKED would auto-publish approval-required work.
    -- QUEUED, PARKED **or FAILED**: §5 admits evidence FAILED for
    -- VALIDATION_SHA_MISMATCH and ARTIFACT_*, and that verdict is as durable as
    -- the others. Restricting the column to two states would force a promotion
    -- to invent a third answer.
    initial_state         TEXT NOT NULL,              -- ValidatedWorkState (any admitted state)
    initial_failure       TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure, e.g. WORKTREE_AHEAD_OF_VALIDATION
    initial_reason        TEXT NOT NULL DEFAULT '',
    admitted_at           TEXT NOT NULL,
    role_changed_at       TEXT NOT NULL,
    released_at           TEXT NOT NULL DEFAULT ''    -- retention release (§6)
);

-- Exactly one CURRENT evidence per record. Roles are exhaustive and mutually
-- exclusive, so this partial index has no complement to leak through.
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_current_evidence
    ON validated_work_evidence (record_id) WHERE role = 'current';

CREATE INDEX IF NOT EXISTS ix_validated_work_evidence_role
    ON validated_work_evidence (record_id, role, admitted_at);

-- Append-only publish attempts (§4.4e). The row is written BEFORE the remote call.
CREATE TABLE IF NOT EXISTS validated_work_publish_attempts (
    record_id             TEXT NOT NULL
                          REFERENCES validated_work_records(record_id),
    attempt_no            INTEGER NOT NULL,           -- 1-based, contiguous
    evidence_id           TEXT NOT NULL,
    target_head_sha       TEXT NOT NULL,
    expected_remote_head  TEXT NOT NULL DEFAULT '',
    phase                 TEXT NOT NULL,              -- DispositionPhase at claim time
    fence                 INTEGER NOT NULL,           -- the owner_fence that claimed it
    started_at            TEXT NOT NULL,
    outcome               TEXT NOT NULL DEFAULT '',   -- '' = no outcome yet (in flight or crashed)
    failure               TEXT NOT NULL DEFAULT '',
    finished_at           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (record_id, attempt_no)
);

-- The activity/reset probe's index. UNRESOLVED includes 'failed'.
CREATE INDEX IF NOT EXISTS ix_validated_work_unresolved
    ON validated_work_records (issue_number, state)
    WHERE state IN ('queued', 'parked', 'publishing', 'failed');

-- At most one drainable row per issue+branch lineage (§2.1.4).
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_lineage_head
    ON validated_work_records (lineage_key)
    WHERE state IN ('queued', 'publishing');

CREATE INDEX IF NOT EXISTS ix_validated_work_lineage
    ON validated_work_records (lineage_key, state);

-- What has actually been published for this issue+branch (§2.1.4). Advanced by
-- BOTH verified publication routes: our own push, and an observed merged PR.
CREATE TABLE IF NOT EXISTS validated_work_lineage (
    lineage_key                 TEXT PRIMARY KEY,
    published_head_sha          TEXT NOT NULL DEFAULT '',
    published_by_record_id      TEXT NOT NULL DEFAULT '',
    published_via               TEXT NOT NULL DEFAULT '',  -- PublicationProvenance
    published_pre_push_expected TEXT NOT NULL DEFAULT '',  -- '' under OBSERVED_MERGE
    published_at                TEXT NOT NULL DEFAULT ''
);

-- Successors parked behind a publishing predecessor (§2.1.4).
CREATE INDEX IF NOT EXISTS ix_validated_work_waiters
    ON validated_work_records (waits_on_record_id)
    WHERE waits_on_record_id != '';

CREATE INDEX IF NOT EXISTS ix_validated_work_issue
    ON validated_work_records (issue_number, state);
"""
