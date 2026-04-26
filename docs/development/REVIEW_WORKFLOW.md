# Review Workflow

## The Review Loop

The core concept: an agent codes, a reviewer checks, and they iterate until the code is approved or the orchestrator escalates.

This flow begins after validation passes. When an agent completes work, the orchestrator processes the completion record — which includes running the validation gate (tests, linting, architecture checks). Only after validation succeeds does the review loop start.

```mermaid
flowchart TD
  DONE["Agent completes work"] --> VAL{"Validation gate"}
  VAL -->|fails| BLOCKED["Blocked — validation failed"]
  VAL -->|passes| REVIEW["Reviewer checks code"]
  REVIEW -->|approved| PR["PR created / marked reviewed"]
  REVIEW -->|changes needed| REWORK["Agent reworks"]
  REWORK --> REVIEW
  REWORK -->|cycle limit hit| ESCALATE["Escalated — needs human"]
```

**Cycle limits prevent infinite loops.** The orchestrator tracks rework iterations (`rework-cycle-N` labels on GitHub). After `max_rework_cycles` (default: 5), it stops the loop and escalates to a human. For in-process exchange modes, `max_rounds` and `max_no_progress` provide additional stopping conditions — if the reviewer reports no progress for consecutive rounds, the loop stops early.

## Exchange Mechanisms

The review loop can run through different mechanisms. The orchestrator selects the mechanism based on `review.exchange.mode` configuration.

### via-draft-pr

Traditional GitHub-based flow. The orchestrator creates a draft PR, launches a reviewer agent against it, and uses GitHub labels to drive the loop. No human intervention required — the orchestrator detects label changes and launches rework sessions automatically.

```mermaid
flowchart LR
  CODE["Agent codes"] --> PUSH["Push + draft PR"]
  PUSH --> REV["Reviewer agent checks PR"]
  REV -->|approves| LABEL1["Label: code-reviewed"]
  REV -->|requests changes| LABEL2["Label: needs-rework"]
  LABEL2 --> REWORK["Orchestrator launches rework session"]
  REWORK --> REV
```

### via-local-loop (default)

Coder and reviewer agents alternate within the orchestrator process. Faster iteration — no GitHub round-trips. The orchestrator runs both agents sequentially and passes results between them.

```mermaid
flowchart LR
  CODE["Coder agent"] -->|submits code| REV["Reviewer agent"]
  REV -->|feedback| CODE
  REV -->|approved| DONE["Create PR + mark code-reviewed"]
```

Stops when: reviewer approves, `max_rounds` reached, or `max_no_progress` consecutive rounds without improvement.

### via-mcp

Coder and reviewer communicate directly via MCP (Model Context Protocol). Same stopping conditions as via-local-loop, but agents exchange messages bidirectionally rather than through the orchestrator.

### auto

Selects `via-mcp` if both agents support it, otherwise falls back to `via-local-loop`.

## Multi-Stage Review Pipeline

After the review loop approves code, additional stages can run.

```mermaid
flowchart TD
  LOOP["Review loop approves code"] --> CR["Code-reviewed"]
  CR --> TRIAGE{"Triage batch review configured?"}
  TRIAGE -->|yes, threshold met| TR["Triage agent reviews patterns across PRs"]
  TR --> DONE["Triage-reviewed — ready for human merge"]
  TRIAGE -->|no| DONE2["Ready for human merge"]

  FAIL["Session failed / blocked / timeout"] --> TFAIL{"triage_review_on_failure?"}
  TFAIL -->|yes| INVEST["Triage agent investigates failure"]
  TFAIL -->|no| SKIP["No investigation"]
```

## Label State Transitions

Labels are the source of truth for issue state. The orchestrator recovers from crashes by reading labels — no database required.

```mermaid
stateDiagram-v2
  state "in-progress" as IP
  state "pr-pending" as PR
  state "needs-code-review" as NCR
  state "code-reviewed" as CR
  state "needs-rework" as NR
  state "triage-reviewed" as TR
  state "blocked-needs-human" as BNH

  [*] --> IP : session launched
  IP --> PR : coding-done completed, PR created
  PR --> NCR : review queued
  NCR --> CR : reviewer approves
  NCR --> NR : reviewer requests changes
  NR --> IP : rework session launched
  IP --> PR : rework completes
  CR --> TR : triage batch review passes
  NR --> BNH : max rework cycles exceeded
  TR --> [*] : human merges
  CR --> [*] : human merges (no triage configured)
```

## Configuration

```yaml
review:
  enabled: true
  default: "agent:reviewer"            # Default reviewer agent

  # Exchange mechanism
  exchange:
    mode: "via-local-loop"             # via-local-loop, via-draft-pr, via-mcp, auto
    loop:
      max_rounds: 10                   # Max iterations (local-loop / mcp)
      max_no_progress: 2              # Stop if reviewer reports no progress N times
      require_validation: true         # Reviewer must confirm validation passed

  # Rework cycle limit (via-draft-pr mode)
  max_rework_cycles: 5                # Escalate to needs-human after N cycles

  # Triage batch review
  triage_review_agent: "agent:triage"
  triage_review_threshold: 5           # Trigger after N code-reviewed PRs
  triage_review_on_failure: true       # Investigate failures (default: true)
```

## Key Design Decisions

1. **Orchestrator manages workflow** - Agents are workers with simple jobs. Orchestrator triggers the right agent at the right time.

2. **Two trigger modes**:
   - **Immediate (in-memory)**: Work agent completes -> orchestrator queues code review
   - **Recovery (label-based)**: On startup, scans for PRs with `needs-code-review` label

3. **Labels as source of truth** - Crash-safe: labels persist, orchestrator picks up where it left off

## Review Decision Policy (Strict)

Review decision criteria are maintained in `.claude/skills/review-workflow/SKILL.md` (canonical source).
Use that section for nit vs non-nit examples and strict approve/request-changes rules.

## Orchestrator Methods

| Method | Purpose |
|--------|---------|
| `queue_code_review()` | Queue PR for review (called on work completion) |
| `launch_review_session()` | Launch review agent for a PR |
| `process_pending_reviews()` | Process queued reviews (each loop) |
| `scan_needs_rework_prs()` | Scan for PRs needing rework |
| `launch_rework_session()` | Launch work agent to fix issues |
| `check_triage_review_trigger()` | Check if triage should trigger |

## Cleanup Configuration

Control when AI session tabs close and worktrees are removed:

```yaml
cleanup:
  with_triage:                    # When triage review is enabled
    close_ai_session_tabs: true   # Close tabs after triage review
    remove_worktrees: false       # Keep worktrees for reference

  without_triage:                 # When triage review is NOT enabled
    wait_for_code_review: true    # true = after code review, false = on completion
    close_ai_session_tabs: true
    remove_worktrees: false
```

## UI Phase Detection

Dashboard shows "Coding" or "Reviewing" based on session terminal ID:
- `issue-*` -> "Coding"
- `review-*` -> "Reviewing"
