# Execution

**Purpose**: Runtime adapter implementations and infrastructure wiring.

**Boundaries**:
- Implements port interfaces for runtime use
- Contains adapter implementations that wire to `adapters/` internals
- Provider factories and dependency injection setup
- SSE broadcast, JSON stores, verification services

## Run-Asset Execution Ownership

- Execution owners allocate and persist run artifacts, then pass typed contracts
  to lower-level collaborators. Collaborators must not rediscover active
  `run_dir` values by scanning the worktree.
- Persistent review exchanges must keep pair-scoped process paths distinct from
  run-scoped artifacts. A live pair's process environment is spawned for one
  `ReviewExchangeRunAssets` binding; do not rebind it to another run.
- Release and respawn persistent review-exchange pairs when the requested run
  binding differs from the pair's spawn-time binding. Reusing a live process
  with stale `RUN_DIR`, `SESSION_ID`, or validation-output env is a correctness
  bug even when pair-scoped recording files are healthy.
- Any best-effort historical inspection must be named and isolated from active
  session, completion, and review-exchange control flow.
