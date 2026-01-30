# Goal Pilot (Goal-Level Operator)

## Summary
Goal Pilot is a goal-level system **whose core is an AI agent**, driving outcomes to completion by orchestrating issue sessions, reviews, and merges. It requires an explicit set of goals as input. It can pivot its plan as new information emerges, and it can use milestones as inputs rather than a fixed sequence. It operates as a controller in the control plane and uses ports to execute actions, keeping all UI/UX surfaces decoupled.

This is intentionally **not** a new UI. The Pilot is an AI control capability that can be invoked by CLI, web UI, or external agent sidecars through a thin adapter.

## Goals
- Provide a goal-oriented control loop: "finish milestones X and Y" or "make the UI clear and easy to navigate."
- Remain UI-agnostic; CLI is just one adapter.
- Reuse existing orchestrator workflows (triage/review/rework) and labels as source of truth.
- Be deterministic and idempotent, with explicit stop conditions.

## Non-Goals
- Replace existing orchestrator control loops.
- Introduce new policy or modify issue lifecycles.
- Embed business logic into CLI or UI.

## Key Concepts
- **Goal**: A desired outcome (e.g., "UI should be clear and easy to navigate").
- **Milestone Set**: One or more milestones used as inputs, not a fixed waterfall.
- **Milestone**: Issue grouping from tracker (GitHub milestone or label set).
- **Pilot Run**: The persistent state of a goal execution loop.
- **Action**: A single, safe, idempotent step taken by the Pilot.
- **Done**: All issues closed, PRs merged, no needs-rework, validations green.

## Architecture Placement
The Pilot fits into the existing hexagonal design:

- **Observation**: Reads issue state, labels, sessions, reviews, and validations.
- **Control**: Decides next action toward goal completion.
- **Execution**: Performs actions via ports (IssueTracker, SessionRunner, etc.).

### Proposed Modules (Control Plane)
- `control/pilot/goal_planner.py`: Resolve goals -> issue sets, define done criteria.
- `control/pilot/goal_controller.py`: Decide next action based on observed state.
- `control/pilot/goal_state.py`: Types for goal-run status and actions.

### Proposed Ports
Keep ports minimal and UI-agnostic:

```
GoalPilot
  - create(goal_spec) -> run_id
  - status(run_id) -> summary
  - next_action(run_id) -> action
  - step(run_id) -> action_result
  - finish(run_id) -> verification
```

Adapters can implement these for:
- CLI (`entrypoints/cli.py`)
- Web UI / control center
- External agent sidecar (Codex/Claude Code)

### Event Model (for UI/agents)
Emit trace events so UIs and sidecars stay decoupled from logs:
- `GOAL_PILOT_CREATED`
- `GOAL_PILOT_UPDATED`
- `GOAL_PILOT_ACTION_PROPOSED`
- `GOAL_PILOT_ACTION_EXECUTED`
- `GOAL_PILOT_ACTION_FAILED`
- `GOAL_PILOT_COMPLETED`

## Control Loop
1. **Observe**: current state of milestone issues, sessions, PRs, validations.
2. **Decide**: choose the next action (triage, dispatch, review, merge, retry).
3. **Act**: execute one action (idempotent, small scope).
4. **Verify**: confirm state transition; update pilot state.
5. **Repeat**: until done or blocked.

## Context Limits and External Memory
Goal Pilot must assume bounded agent context. The design therefore relies on durable, external memory rather than in-session prompts.

### Memory Requirements
- **Run state**: current phase, last action, pending actions, retries.
- **Milestone snapshots**: issue lists, labels, PR status, validations.
- **Decision summaries**: rationale for actions taken and why something is blocked.
- **Skills**: reusable patterns the agent should apply automatically.

### Storage Strategy
- Persist state via `GoalPilotStore` (SQLite adapter) so restarts do not lose context.
- Store compact summaries, not raw logs. Keep large artifacts in existing orchestrator stores (session logs, event streams).
- Use deterministic, monotonic updates (append-only events + derived summaries) to avoid partial state.

### Skills: Dual-Layer Memory
Goal Pilot uses a dual-layer approach to leverage native AI skills without bloating context:
1. **Durable store (SQLite)**: authoritative record of discovered skills.
2. **Manifested skills**: YAML files generated from the DB for native AI use.

The store remains source-of-truth; the YAML files are a full projection for active skills.

### Context Hygiene
- Each `next_action` should rely on stored summaries + fresh observations, not full history.
- The Pilot should periodically emit a condensed "run summary" event for external agents.
- Avoid recomputing full milestone state when a cached snapshot is fresh and validated.

## Action Policy (Examples)
- If issues are untriaged -> run triage workflow.
- If capacity is available -> dispatch new sessions.
- If sessions completed -> start review workflow.
- If reviews approved -> merge per policy.
- If blocked -> mark blocked and halt or escalate.
- If goal metrics are not improving -> pivot (re-scope issues, reprioritize, change approach).

## Goal Pilot Action Types
These are explicit, auditable commands the AI agent can propose:
- `create_issue`
- `create_milestone`
- `reassign_issue_to_milestone`
- `reprioritize`
- `defer_issue`
- `change_approach`
- `dispatch`
- `review`
- `merge`
- `triage`
- `noop`

## Goal Pilot Autonomy (Required Capabilities)
Goal Pilot must be allowed to change course without a full restart. These are first-class actions:

1) **Reprioritize mid-flight**
   - Example: "Issue #45 revealed we need #78 first."
   - Effect: update ordering, pause/stop lower-priority sessions, dispatch prerequisites.

2) **Spawn new work**
   - Example: "Found a bug while implementing, need to track it."
   - Effect: create a new issue (or task) and add it to the goal scope.

3) **Defer / descope**
   - Example: "This is bigger than expected, punt to next milestone."
   - Effect: move items out of scope and record rationale.

4) **Change approach**
   - Example: "Original plan won't work, pivoting."
   - Effect: update goal strategy and replan issue set/sequence.

## Overlap with Triage Agent
The triage agent remains a specialized workflow. Goal Pilot uses it as a subroutine. The Pilot is a higher-level controller; triage is one action among many.

## pilotCLI Surface (Adapter Only)
pilotCLI is optional. It should map 1:1 to the port API:
- `pilot goal create --goals "UI clear and easy to navigate" --milestones 1,2`
- `pilot goal status --id <run_id>`
- `pilot goal next --id <run_id>`
- `pilot goal step --id <run_id>`
- `pilot goal finish --id <run_id>`

## Control API (initial)
The control API is the first interaction surface:
- `POST /control/goal_pilot/runs` (create run)
- `GET /control/goal_pilot/runs/{run_id}` (status)
- `PATCH /control/goal_pilot/runs/{run_id}` (update goals)
- `POST /control/goal_pilot/runs/{run_id}/actions` (execute action)
- `GET /control/goal_pilot/skills` (list skills)
- `POST /control/goal_pilot/skills` (upsert skill)
- `POST /control/goal_pilot/skills/export` (export YAML manifest)

These commands must stay thin: they call the Pilot port and print results.

## Storage
Persist pilot run state via a dedicated store with SQLite as the primary adapter:
- `GoalPilotStore` (port) -> `SqliteGoalPilotStore` (adapter)
  - JSON adapter can exist for tests or local debugging, but SQLite is the durable source of truth.

### Storage Schema (SQLite)
SQLite provides compact, durable external memory with good read performance and crash safety.

**Tables**
```
goal_pilot_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,          -- active | blocked | completed | failed
  goals_json TEXT NOT NULL,      -- list of goal statements
  done_criteria_json TEXT NOT NULL
)

goal_pilot_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_hash TEXT NOT NULL,     -- hash of observed inputs to avoid duplicate snapshots
  summary_json TEXT NOT NULL,    -- compact milestone summary
  FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
)

goal_pilot_actions (
  action_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  action_type TEXT NOT NULL,     -- triage | dispatch | review | merge | retry | block | finish
  input_json TEXT NOT NULL,      -- parameters used for the action
  result_json TEXT NOT NULL,     -- outcome + artifacts
  status TEXT NOT NULL,          -- proposed | executed | failed | skipped
  FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
)

goal_pilot_notes (
  note_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note_type TEXT NOT NULL,       -- decision | block_reason | summary
  note_text TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES goal_pilot_runs(run_id)
)
```

**Indexes**
```
CREATE INDEX idx_runs_status ON goal_pilot_runs(status);
CREATE INDEX idx_actions_run_id ON goal_pilot_actions(run_id);
CREATE INDEX idx_snapshots_run_id ON goal_pilot_snapshots(run_id);
```

**Skills Table**
```
goal_pilot_skills (
  skill_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,          -- draft | active | deprecated
  title TEXT NOT NULL,
  intent TEXT NOT NULL,
  triggers_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL,
  playbook TEXT NOT NULL,
  examples_json TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  last_verified TEXT
)
```

**Retention / Compaction**
- Keep all actions (they are small and audit-friendly).
- Periodically compact snapshots by keeping the latest per `source_hash`.
- Emit and persist a periodic "summary" note to avoid rereading full action history.

## Safety Model
- Idempotent actions (safe to retry).
- Allowed action list per pilot run.
- Rate limiting and exponential backoff.
- Dry-run option (no side effects).
- Explicit stop conditions (blocked, invalid state, safety triggers).

## Minimal Implementation Plan
1) **Domain + Control**
   - Add goal run types and action model.
   - Implement `GoalPilot` control logic and state machine.

2) **Ports + Adapters**
   - Add `GoalPilot` port and SQLite store.
   - Wire in `bootstrap.py`.

3) **Entry Points**
   - Add thin CLI adapter.
   - Emit pilot events for UI/agents.

4) **Tests**
   - Unit tests for decision policy and action sequencing.
   - Integration test with a minimal fake IssueTracker.
