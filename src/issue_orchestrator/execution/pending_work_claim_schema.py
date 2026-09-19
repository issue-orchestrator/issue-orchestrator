"""The pending-work ledger's TABLE SHAPES, kept apart from its operations.

Schema text, the additive-column list and the statement splitter are a
different concern from the reads and writes that use them, and the store module
was over its line budget with both in it. Nothing here touches a connection:
callers still own transactions and migration order (#6999 F13).
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_work_claim (
    run_key TEXT PRIMARY KEY,
    work_key TEXT NOT NULL,
    deferred INTEGER NOT NULL DEFAULT 0,
    session_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_work_claim_work
    ON pending_work_claim (work_key);
CREATE TABLE IF NOT EXISTS pending_work_claim_quarantine (
    quarantine_key TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    session_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    error TEXT NOT NULL,
    -- Durable state machine (#6999 F12). ``label_state`` records whether THIS
    -- quarantine actually acquired the shared blocking label or found it
    -- already there, so release can only ever remove a label it added.
    -- ``announced`` is separate because AddLabel can land while the comment
    -- fails. ``releasing`` marks a resolved cause whose cleanup has not yet
    -- committed, so the row survives to be retried.
    label_state TEXT NOT NULL DEFAULT 'unknown',
    announced INTEGER NOT NULL DEFAULT 0,
    releasing INTEGER NOT NULL DEFAULT 0,
    -- The observation the announcement was written for, and the work it names
    -- (#6999 F6). Durable because ``announced`` is: a quarantine re-observed
    -- under a DIFFERENT cause has to rewrite the operator's story, and the only
    -- way to know it changed is to have kept the one that was announced.
    -- Nullable on purpose - a row from before this column read as "no cause
    -- recorded", which must differ from every observable cause so the next
    -- scan re-announces rather than standing on a story nothing vouches for.
    cause TEXT,
    work_kind TEXT
);
-- Durable provenance for every OTHER cause of the shared needs-human block
-- (#6999 F2 round 2). The tech-lead marker label and the quarantine table
-- above already record their own. A session or planner escalation recorded
-- nothing, so a remover saw an owner-less label and took it off. Rows are
-- meaningful only while the label is present and are dropped with it, so a
-- stale one can never strand an issue in needs-human.
-- NOTE: no semicolons in this comment - the schema is split on them.
-- An unacknowledged removal may have committed remotely. Preserve a present
-- label on replay until absence proves the old generation has ended.
CREATE TABLE IF NOT EXISTS needs_human_removal_intent (
    issue_number INTEGER PRIMARY KEY CHECK (issue_number > 0)
);
CREATE TABLE IF NOT EXISTS needs_human_cause (
    issue_number INTEGER NOT NULL,
    cause TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (issue_number, cause)
);
"""

# Additive columns the quarantine table gained after it shipped. ``CREATE TABLE
# IF NOT EXISTS`` leaves an existing table exactly as it is, so a database
# written by an earlier build keeps the old shape and every later statement
# referencing these columns fails. Unlike the claim table this needs no
# all-or-nothing rebuild: quarantines carry no queued work, so a NULL cause is
# recoverable by the next scan re-announcing (#6999 F6).
QUARANTINE_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cause", "TEXT"),
    ("work_kind", "TEXT"),
)

STORE_FILENAME = "pending_work_claims.sqlite"


def schema_statements() -> tuple[str, ...]:
    """The schema as individual statements.

    ``executescript`` commits any pending transaction before it runs, so it
    cannot be used inside the migration's single transaction (#6999 F13).
    """
    return tuple(
        statement.strip()
        for statement in SCHEMA.split(";")
        if statement.strip()
    )
