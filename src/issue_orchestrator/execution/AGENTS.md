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
  run-scoped artifacts. Rebinding a live pair to a current
  `ReviewExchangeRunAssets` is owner work, not caller rummaging.
- Do not respawn or release persistent pairs merely because a run id changed.
  Release only for concrete process/recording/contract failure, missing current
  completion artifacts, timeout, or explicit lifecycle shutdown.
- Any best-effort historical inspection must be named and isolated from active
  session, completion, and review-exchange control flow.
