# ADR 0026: Issue-lifecycle persistent coder/reviewer pair

**Status:** Proposed
**Date:** 2026-05-05

## Context

The "persistent session exchange" cutover (PR #6160) introduced a single subprocess per role that survives **across rounds within one review exchange**: round 1 prompt → reviewer responds → round 2 prompt — same reviewer process, same coder process. Before #6160, every round spawned a fresh agent.

That fixed round-to-round overhead but it did **not** fix exchange-to-exchange overhead. The architectural map (see "Current state" below) confirms what the user observed in the e2e regression: each rework cycle generates a fresh `session_name`, which causes `CompletionReviewExchange.run_review_exchange_loop` → `ReviewExchangeRunner.run` → `run_persistent_session_exchange` to spawn a **brand-new** coder + reviewer pair. A typical issue with three rework cycles ends up with three coder processes and three reviewer processes (six PTY-attached agents) over its lifetime, each one losing the conversation context the previous one built up.

The user's framing of the gap:
> "the entire point is that they persist! ... 1 process coder and 1 process reviewer for the life of the exchanges, e.g. 3 coding exchanges, 1 single coder, 1 single reviewer."

## Current state (as of `8c7f581`)

| Layer | Lifetime today | Lifetime we want |
|---|---|---|
| Coder TUI process | One review exchange | Whole issue, until PR closes / orchestrator stops / process dies |
| Reviewer TUI process | One review exchange | Same as coder |
| Reviewer worktree | One review exchange (created/removed per call) | Whole issue (fast-forwarded between rounds AND between exchanges) |
| `coding-done` / `reviewer-done` exit semantics | Stay alive on stdin, write JSON, wait for next prompt | **Already correct — no change needed** |
| `Session` model scope | Terminal-scoped (`(IssueKey, TaskKind)`) | Unchanged — Session is not the right home for the pair |

**Key insight from the map:** the agents already support what the user wants. `coding_done.py` and `reviewer_done.py` write the JSON response and fall through to wait on stdin — they do not exit. The bootstrap prompt explicitly tells them: *"Wait for the next prompt. Do not exit on your own; the orchestrator will terminate you when the exchange is done."*

The persistence gap is entirely **orchestrator-side**: `run_persistent_session_exchange` spawns inside its own `try`/`finally`, owns the pair, and closes them on return. The caller (`completion_review_exchange`) calls back in with a fresh `session_name` for each rework cycle, missing the cached-summary fast-path, triggering a fresh spawn.

## Decision

**Introduce an issue-scoped `PersistentExchangePairRegistry` that owns the coder + reviewer subprocess pair plus the reviewer worktree, keyed by `IssueKey`. `run_persistent_session_exchange` becomes a *participant* in the pair's life, not its owner.**

Concretely:

1. **New port `PersistentExchangePairRegistry`** (in `ports/`). The
   registry's API expresses *behavior*, not subprocess plumbing.
   Spawn is supplied by the caller so the registry never needs to
   know how an agent is launched (which command, env, recording
   path), and the returned pair carries its sessions as opaque
   handles — concrete `PersistentSession` types live in `execution/`
   and are forbidden from leaking through ports per the
   import-linter "ports must not depend on execution" rule.
   ```python
   class PersistentExchangePairRegistry(Protocol):
       def acquire(
           self,
           *,
           issue_key: Hashable,
           spawn: Callable[[], PersistentExchangePair],
       ) -> PersistentExchangePair: ...

       def release(self, issue_key: Hashable, *, reason: str) -> None: ...

       def shutdown_all(self, *, reason: str) -> None: ...
   ```

2. **`PersistentExchangePair`** holds:
   - `coder_session: Any`, `reviewer_session: Any` — *opaque*
     handles at the port boundary. The execution-layer adapter
     constructs each pair with strictly-typed `PersistentSession`
     instances, but the port stays agnostic so future adapters
     (e.g. a non-PTY backend, a remote-process backend) can plug
     in without touching the port. See the in-tree comment on the
     dataclass for why `Any` rather than a tighter Protocol.
   - `reviewer_worktree_path: Path` (created once, fast-forwarded
     at the start of every reviewer round, removed when the pair
     is released)
   - `coder_response_path`, `reviewer_response_path`,
     `coder_recording_path`, `reviewer_recording_path`,
     `coder_completion_path`, `validation_record_path`:
     pair-scoped paths the agent's env points at. The agent's env
     is set once at spawn; if the pair survives across exchanges,
     every round of every exchange reads/writes the same physical
     files. Per-exchange `run_dir` still holds chapters / summary;
     recordings are mirrored from the pair scope into
     `run_dir/<role>/` for backward compat with tooling that
     watches per-exchange paths.
   - `created_at`, `last_used_at` (for diagnostics + idle reaping)
   - `is_alive() -> bool` — fail-fast probe; if either side died,
     registry evicts and the next caller gets a fresh pair

3. **`run_persistent_session_exchange` change**: instead of
   `_open_role_session` for both roles inline, build a `spawn`
   closure that creates the reviewer worktree, opens both sessions,
   and returns a populated `PersistentExchangePair`; pass the
   closure to `registry.acquire(...)`. The function no longer
   `release`s in `finally` — pair lifetime is now owned by the
   registry's lifecycle hooks (see boundaries below). `last_used_at`
   is refreshed inside `acquire` on cache hit so the registry can
   still drive idle-reaping without an explicit `mark_used` call.

4. **Lifecycle boundaries** (when does a pair die?). These are the
   *single owner* of pair termination — no other path may call
   `release` or close subprocesses directly.

   - **Issue completes (PR merged or issue closed):**
     `release(issue_key, reason="issue-closed")`. The owning code
     is the same place that drops the `in-progress` label.
   - **Orchestrator stops:** `shutdown_all(reason="orchestrator-shutdown")`
     from the bootstrap teardown.
   - **Pair process exits unexpectedly:** registry's `is_alive()`
     check returns False on the next `acquire` call; registry
     evicts the dead pair and spawns fresh. (Same behavior as a
     crash today, just centralized.)
   - **Reset-and-retry from scratch:**
     `release(issue_key, reason="reset-retry")` at the start of
     the reset path. The pair was tied to the prior worktree
     state; we want a clean slate.
   - **Escalation to human:**
     `release(issue_key, reason="escalated-to-human")` from
     `ActionApplier._apply_escalate`. Escalation is **terminal
     for the pair** because the orchestrator's automated retry
     loop has explicitly given up — `EscalateToHumanAction` only
     fires after `max_rework_cycles` is exhausted. If a human
     un-escalates and the orchestrator picks the issue back up,
     the next exchange will spawn a fresh pair (fresh agent
     context is what a human resuming would want anyway). Treating
     escalation as terminal also avoids long-tail PTY agents
     drifting on issues that are no longer the orchestrator's
     responsibility — the "memory cost only" framing in the next
     bullet covers transient idleness within an active issue, not
     escalated-and-handed-off issues.
   - **Idle reaping (optional, deferred):** if the pair sits
     unused for >N minutes *while the issue is still actively
     under orchestrator control*, reap. **Not in scope for the
     first cut** — within an active issue's lifetime the worst
     case is "PTY agents idle while a rework cycle waits on a
     slow validation step or a flaky CI." That's a memory cost,
     not a correctness cost. Escalation and other terminal
     transitions above are handled explicitly, so idle reaping
     does not need to cover them.

5. **Restart-from-labels recovery:** unchanged in semantics. Subprocesses don't survive an orchestrator restart, so on first `get_or_create` after restart the registry sees an empty cache and spawns. The cached `summary.json` mechanism in `completion_review_exchange.load_existing_review_exchange_outcome` is orthogonal and continues to work — if the validation cache is fresh, we never enter `run_persistent_session_exchange` at all.

6. **Reviewer worktree:** moved into `PersistentExchangePair`. Created once per pair. `fast_forward_reviewer_worktree(reviewer_wt, coder_branch_tip)` is called at the start of every reviewer round (existing behavior, reused). Removed in `release()`, not in `run_persistent_session_exchange`'s `finally`.

7. **Composition root**: `entrypoints/bootstrap.py` builds one `PersistentExchangePairRegistry` and threads it into the `ReviewExchangeRunner` adapter. The registry is owned by the orchestrator's lifetime, not by any session.

## Non-goals

- **Surviving an orchestrator process restart.** Subprocesses are tied to the parent's PTY; we are not building a control-plane–style detach/reattach. After restart, the next exchange spawns fresh — that is acceptable per the user's note ("of course if the system crashes, or it is reset and retried I expect new persistent sessions").
- **Cross-issue process pooling.** Pairs are issue-scoped. We are not introducing a worker pool.
- **Changing the `coding-done` / `reviewer-done` agent CLI contract.** They already wait on stdin between rounds. No change to their exit semantics. No round counter envelope. The existing prompt-text-driven directive model is retained.
- **Changing the `Session` domain model.** The pair lives next to `Session`, not inside it. `Session` remains terminal-scoped. (The pair is, in effect, two terminals shared across many `Session` phases for the same issue.)
- **Persistent agents for the *coding* phase outside review exchanges.** Only the review-exchange pair is in scope. The main coder TUI is a separate concern.

## Migration plan

The change is large enough that a single PR is risky. Proposed three-PR series:

**PR B1 — Registry + pair entity, used only inside `run_persistent_session_exchange`'s call site (no behavior change).**
Introduce the registry, but every call still does `get_or_create` followed by `release` in the same exchange. Pair lifetime is still per-exchange. This is a refactor: same external behavior, but the spawn/close logic moves from `run_persistent_session_exchange` into the registry, and the registry is a singleton owned by the orchestrator.
- Smoke test: e2e suite passes unchanged.
- Validates the abstraction without changing user-visible behavior.

**PR B2 — Drop the per-exchange `release`; survive across exchanges within an issue.**
Stop calling `release()` at the end of each exchange. Add
`release()` calls at the canonical lifecycle boundaries enumerated
in section 4 above — orchestrator shutdown (`shutdown_all`),
reset-and-retry, and escalation-to-human. The PR-merged /
issue-closed boundary may land in a follow-up if no single
canonical owner exists in the codebase yet; the pair is bounded
in the worst case by orchestrator shutdown. This is the change
that delivers what the user asked for.
- New tests: integration test that runs two-or-more review
  exchanges back-to-back for one issue and asserts the same coder
  PID and the same reviewer PID across all of them.
- New tests: registry evicts and respawns when a process dies
  between exchanges.
- E2E sweep: confirm the reset-retry-from-scratch test still
  works (it kills the issue's pair as part of reset).
- Path-scope refactor: the agent's env points at *pair-scoped*
  paths under `<state_dir>/persistent-pairs/issue-<n>/{coder,reviewer}/...`
  so a cached pair handed to exchange 2 reads/writes the same
  files exchange 1 did. Per-exchange `run_dir` keeps `chapters.json`
  and `summary.json`; recordings are mirrored from pair scope into
  `run_dir/<role>/` for backward compat.

**PR B3 — Diagnostics + observability.**
Add control-API endpoints for `GET /control/exchange-pairs` returning current pairs (issue_key, coder_pid, reviewer_pid, age, last_used_at). Add a UI badge on the issue card showing "persistent pair: alive / restarted / never spawned". Add structured events for pair lifecycle transitions.
- Useful for confirming the user-visible benefit landed and for debugging future regressions.

## Risks

| Risk | Mitigation |
|---|---|
| Long-lived TUI agents leak memory | Use `is_alive()` to evict dead pairs; deferred idle-reaping if memory becomes a concern |
| Coder agent's accumulated context drifts (stale assumptions about repo state from earlier rounds) | This is the user's *intent* — they want the agent to retain context. If a specific exchange needs a fresh start, the caller can `release()` first. |
| Reviewer worktree gets out of sync between exchanges | Fast-forward at the start of every reviewer round (already the contract within an exchange) — unchanged, just applies across exchanges too |
| Registry not torn down on orchestrator stop | Wire `shutdown_all` into bootstrap teardown; integration test asserts no orphan pids after orchestrator stop |
| Reset-retry leaves a stale pair from the old attempt | Explicit `release(issue_key, reason="reset-retry")` at the start of the reset path |

## Consequences

**Positive**
- Matches the user's mental model of "persistent" — one coder, one reviewer, for the life of an issue's review exchanges.
- Eliminates the spawn cost of three full agent boots per typical 3-rework issue.
- Coder + reviewer retain conversation context across rework, which is the whole point of an interactive agent loop.
- Diagnostics get easier — a single PID per role per issue, not one per exchange.

**Negative**
- A wedged agent now wedges *all* of an issue's exchanges, not just one. Mitigated by the `send_round` heartbeat + timeout from #6205, plus `is_alive()` eviction in the registry.
- Memory footprint grows with concurrent issues. Two extra TUI agents per active issue. Bounded by the orchestrator's existing concurrency limit; deferred idle-reaping if it bites.
- The registry is shared mutable state, which the architecture rules call out as needing an owner abstraction. The registry **is** the owner abstraction; no entrypoint touches the underlying `dict[IssueKey, PersistentExchangePair]` directly.

**Neutral**
- The `coding-done` / `reviewer-done` CLIs are unchanged. Their already-correct stay-alive-on-stdin semantics are what makes this redesign cheap.
- Cached-summary recovery (`load_existing_review_exchange_outcome`) keeps working as-is; the registry sits on the path **after** the cache check.

## Final abstraction pass

1. **Policy scattered across call sites** — Today, "spawn coder, spawn reviewer, close both" is policy embedded in `run_persistent_session_exchange`. The redesign concentrates that policy in the registry. ✓ Resolved.
2. **Entrypoints touching internals** — No entrypoint will touch the registry's dict directly; the registry exposes `get_or_create` / `release` / `shutdown_all`. ✓ Resolved.
3. **Shared mutable state** — `dict[IssueKey, PersistentExchangePair]` is owned by the registry. ✓ Resolved.
4. **Callers needing knowledge of multiple internals** — `run_persistent_session_exchange` no longer needs to know about subprocess spawn or worktree creation; it asks the registry. ✓ Resolved.
5. **Cross-path drift** — Single owner means the rules are enforced once. ✓ Resolved.

Final abstraction pass: no issues found.
