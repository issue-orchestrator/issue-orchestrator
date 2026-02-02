---
name: sqlite-ops
description: SQLite usage, backup/care, WAL mode, and corruption handling in issue-orchestrator. Use when answering questions about SQLite configuration, persistence, backup/recovery, or when modifying sqlite3 usage in this repo.
---

# Sqlite Ops

## Overview

Locate SQLite usage in issue-orchestrator and provide practical, low-maintenance backup and corruption-handling guidance tailored to the repo's databases.

## Workflow

1. Identify which SQLite DB(s) are in scope.
   - Load `references/issue-orchestrator-sqlite.md` for paths and owners.
2. Confirm the runtime context (local desktop vs server, DB size, write frequency, WAL or not).
3. Recommend a safe backup method:
   - If DB may be open: use `VACUUM INTO` or sqlite3 `.backup` (avoid raw file copy).
   - If DB can be closed: stop app, then copy the DB file (and `-wal`/`-shm` if WAL) or still use `VACUUM INTO` for a clean snapshot.
4. Suggest durability settings:
   - WAL mode for concurrency and crash safety.
   - `PRAGMA synchronous=NORMAL` for performance or `FULL` for max durability.
   - Periodic checkpoints to avoid WAL bloat.
5. Corruption response:
   - Run `PRAGMA quick_check` on startup.
   - On failure: move DB to `*.corrupt`, restore latest backup, and surface a clear alert.
6. Recovery guidance:
    - Identify latest backup in `.issue-orchestrator/backups/sqlite/<db_key>/daily/` (or weekly).
    - Stop orchestrator, replace DB file with backup, restart.

## Repo Notes

- JobStore already sets WAL mode on its connection; E2EDB and subprocess registry do not.
- Subprocess registry includes auto-corruption handling; the others do not.

## Resources

### references/

Use `references/issue-orchestrator-sqlite.md` for DB locations, connection settings, and current corruption handling.
