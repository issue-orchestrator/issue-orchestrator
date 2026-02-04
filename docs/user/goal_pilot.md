 # Goal Pilot (User Guide)
 
 Goal Pilot is a goal-level AI controller for issue-orchestrator. It helps you steer a project by
 working from outcomes and critical user journeys, then proposing concrete repository actions
 (issues, milestones, labels, etc.). It is designed to run in short, repeatable cycles so the AI can
 do focused thinking without long-lived context.
 
 This guide is for humans and for fresh AIs that need to get productive quickly.
 For design details and schema background, see `docs/design/goal-pilot.md`.
 
 ---
 
 ## Mental Model
 
 Goal Pilot is a loop with durable memory:
 
 1. **You define intent**: goals + done criteria.
 2. **Goal Pilot structures the work**: phases + critical journeys.
 3. **Goal Pilot proposes changes**: repo actions or approach updates.
 4. **You gate actions** (per approval policy).
 5. **Memory is updated** in SQLite + skills, so the AI can forget context and continue later.
 
 The loop is explicitly designed to avoid “waterfall” behavior. Plans are allowed to pivot.
 
 ---
 
 ## Core Concepts
 
 - **Run**: A named session for a set of goals. Multiple runs are supported.
 - **Goals**: Short, human-readable outcomes.
 - **Done criteria**: Structured completion conditions (JSON).
 - **Phase**: High-level stage (ex: outcomes/opportunities, critical journeys, architecture, execution).
 - **Journey**: A critical user journey (CUJ) with order, priority, and “under the covers” needs.
 - **Suggested changes**: Proposed repo actions (issues, milestones, labels, or approach changes).
 - **Skills**: Reusable heuristics. Stored in SQLite; can be exported as YAML for AI skill loading.
 - **Notes**: Rationale and decisions (for later grounding).
 
 ---
 
 ## External Memory (SQLite + Skills)
 
 Goal Pilot stores durable state in `goal_pilot.sqlite` under the repo state dir.
 This is the source of truth for:
 
 - Runs, goals, phases, notes
 - Journeys and their ordering
 - Suggested changes / actions
 - Skill entries
 
 The system can export skills to YAML for AI usage (optional), but the database remains the
 source of truth.
 
 ---
 
 ## Approval Policies (How Actions Apply)
 
 - `journeys_only` (default): Goal Pilot can structure journeys and phases, but repo changes
   are only proposed.
 - `gatekeeper`: Human approves each change (fine-grained).
 - `batch`: Changes are bundled and approved in batches.
 
 The control center UI will show the current policy and whether Goal Pilot is configured.
 
 ---
 
## How to Use (Minimal)
 
1. **Create a run**: give it a name, goals, and done criteria.
2. **Define journeys**: capture critical user journeys and their ordering.
3. **Set phase**: record which stage you are in (mapping, architecture, execution).
4. **Review suggested changes**: approve or revise, per policy.
 
---

## Goal Interview (Recommended Before Creating a Run)

For projects with high stakes or ambiguity, run a **goal interview** first. This is a guided,
adaptive Q&A that clarifies outcomes, constraints, and success criteria before execution.

**Why**: It reduces rework and gives you a durable record of how goals evolved.

**What it covers**
- Intent + vision
- Users + stakeholders
- Constraints (hard/soft)
- Success criteria (metrics + qualitative)
- Risks + unknowns
- Priority + phasing
- User-active prompt for anything else to discuss

**What it can change**
- Merge goals with the same outcome
- Split goals with conflicting success criteria
- Drop goals that are out of scope or unclear

**Confirmation step**
Goal Pilot should present a short recommendation list with score breakdowns and ask
for explicit confirmation before changes are applied.

```
Recommendation: Drop G-006
Why: Clarity = 0, Impact = 1, Feasibility = 2, Risk = 1 (Total 4)
Rule: Drop if total <= 4 or clarity = 0
Notes: No measurable success criteria defined
```

---

## Assist Mode (Outcome Suggestions)

Assist mode offers optional **outcome-level** prompts based on patterns from similar systems.
It does not propose implementation details. Accepted outcomes become inputs for the system
to translate into lower-level work automatically.

**Example prompts**
- Should users be able to restore data if they make mistakes?
- Do you want a clear audit trail for critical actions?
- Should users be able to export their data?

Record accepted or rejected outcomes as notes, then proceed to execution.

**Research permission**
If identifying reuse options requires current market knowledge, Goal Pilot should ask
for permission before performing web research.

---

## Reuse Over Reinvent

Goal Pilot should strongly prefer reuse over bespoke solutions. When planning actions,
it should check for existing software (open source or paid) that covers non-differentiating
needs, and focus custom work on what is unique.

---

## Short-Cycle AI Pattern (Recommended)

Goal Pilot is meant to be run like a “thinking loop” with minimal context retention:
 
 1. **Prepare context packet** (from DB + brief user input):
    - Active run + goals + phase
    - Ordered journeys
    - Recent notes and decisions
    - Proposed changes (open items)
 
 2. **Ask the AI for a focused output**:
    - *“Given the current phase and journeys, propose the next 1–3 changes and explain why.”*
 
 3. **Persist output**:
    - Store the AI’s suggestions in actions/notes
    - Update journeys or phase if needed
    - Update skills if a durable heuristic was discovered
 
 4. **Wait**: end the cycle, no long memory required.
 
This mirrors the issue-orchestrator pattern: let AI do short, discrete work with no
environment or git management, then persist the result.

---

## Wake → Assess → Decide → Act (Heartbeat Loop)

Goal Pilot should run on a short heartbeat (every N minutes), but **not** force action.
Each wake-up answers:

- Are we hewing to the goals?
- Are we making progress on the top journeys?
- If not, why — and do we need a deeper inspection?

Outcomes:

- **Act**: propose 1–3 changes.
- **Deep inspection**: re-evaluate goals, journeys, and under-the-covers.
- **Sleep**: no meaningful move; wait for next wake.

---

## Discrete Task Layers (What “Small” Means)

Goal Pilot operates on bounded tasks at multiple abstraction layers:

1. **Goals**: adjust goals or done criteria.
2. **Journeys (CUJs)**: define/adjust ordering, priority, success criteria.
3. **Under the covers**: architecture, guardrails, tests, integration points.
4. **Repo actions**: issues, milestones, labels, reprioritization.

Each cycle should pick one layer and output only a few concrete updates.

---

## Deep Inspection (Empowered to Change)

Deep inspection is a special cycle that **can change structure**, not just observe.
It may:

- Reorder or replace journeys
- Update goals or done criteria
- Insert missing under-the-covers work (tests/guardrails/architecture)
- Propose repo actions (still gated by approval policy)

It should end with a clear decision log: what changed and why.

---

## Suggested Output Schema (AI → Goal Pilot)

The AI should return a structured response (example shape):

```
{
  "mode": "shallow | deep",
  "phase_change": {"to": "critical_journeys", "reason": "..."},
  "journey_updates": [
    {"title": "Onboard new user", "priority": "high", "order_index": 0, "under_the_covers": {...}}
  ],
   "suggested_changes": [
     {"action_type": "create_issue", "title": "...", "body": "...", "labels": ["goal-pilot"]}
   ],
  "skills_to_upsert": [
    {"title": "...", "intent": "...", "triggers": [...], "constraints": [...], "playbook": "..."}
  ],
  "notes": ["Why this is next", "Risks", "Dependencies"]
}
```
 
 Keep it small and explicit. Avoid multi-page plans.
 
 ---
 
 ## Configuration (YAML)
 
 ```
 goal_pilot:
   enabled: true
   agent: agent:goal-pilot
   approval_policy: journeys_only  # journeys_only | gatekeeper | batch
   approval_batch_size: 10
   approval_batch_window_minutes: 60
 ```
 
 ---
 
 ## Control API (High Level)
 
 - Create run: `POST /control/goal_pilot/runs`
 - List runs: `GET /control/goal_pilot/runs`
 - Run status: `GET /control/goal_pilot/runs/{run_id}`
 - Update phase: `POST /control/goal_pilot/runs/{run_id}/phase`
 - Journeys:
   - List: `GET /control/goal_pilot/runs/{run_id}/journeys`
   - Create: `POST /control/goal_pilot/runs/{run_id}/journeys`
   - Update: `PATCH /control/goal_pilot/journeys/{journey_id}`
   - Reorder: `POST /control/goal_pilot/runs/{run_id}/journeys/reorder`
 - Skills:
   - List: `GET /control/goal_pilot/skills`
   - Upsert: `POST /control/goal_pilot/skills`
   - Export: `POST /control/goal_pilot/skills/export`
 
 ---
 
 ## How This Helps AIs Onboard Quickly
 
 - The run, journeys, and notes act as a compact “briefing.”
 - The AI only needs a small packet of context each cycle.
 - Long-term learning becomes skills in the DB (and optionally YAML).
 
 This means a fresh AI can open the Goal Pilot panel, read the run status,
 and immediately propose the next best action without reading the whole repo.
