# Architecture

For architecture principles, coding conventions, dependency injection patterns, event vs logging rules, testing philosophy, and port/adapter guidelines, see [CLAUDE.md](../../CLAUDE.md). That file is the single source of truth for how to work in this codebase — it's written for AI agents but applies equally to human developers.

## System Overview

```mermaid
graph TB
    subgraph "Entry Points"
        CLI[CLI<br/>issue-orchestrator]
        WEB[Web UI<br/>localhost:8765]
    end

    subgraph "Control Plane"
        ORCH[Orchestrator]
        SCHED[Scheduler]
        PLAN[Planner]
        APPLY[ActionApplier]
        OBS[Observer]
    end

    subgraph "Domain"
        MOD[Models]
        EVT[Events/Catalog]
        DEP[Dependencies]
    end

    subgraph "Ports (Interfaces)"
        PT_REPO[RepositoryHost]
        PT_SESS[SessionRunner]
        PT_EVT[EventSink]
        PT_WC[WorkingCopy]
        PT_WT[WorktreeManager]
        PT_CMD[CommandRunner]
        PT_STORE[SessionStore]
    end

    subgraph "Adapters (Execution)"
        GH[GitHubAdapter]
        TERM[TerminalAdapter<br/>subprocess]
        SSE[SSE Plugin]
        WT[WorktreeAdapter]
        STORE[JsonSessionStore]
    end

    subgraph "External Systems"
        GHAPI[GitHub API]
        TERMS[Terminal Sessions]
        BROWSER[Browser SSE]
        FS[Filesystem]
    end

    CLI --> ORCH
    WEB -->|REST API| ORCH
    WEB -->|SSE| SSE

    ORCH --> SCHED
    ORCH --> PLAN
    ORCH --> OBS
    PLAN --> APPLY

    SCHED --> DEP
    PLAN --> MOD
    OBS --> EVT

    APPLY --> PT_REPO
    APPLY --> PT_SESS
    APPLY --> PT_EVT
    OBS --> PT_WC
    ORCH --> PT_WT
    ORCH --> PT_STORE

    PT_REPO --> GH
    PT_SESS --> TERM
    PT_EVT --> SSE
    PT_WT --> WT
    PT_STORE --> STORE

    GH --> GHAPI
    TERM --> TERMS
    SSE --> BROWSER
    WT --> FS
    STORE --> FS

    style WEB fill:#6366f1,color:#fff
    style CLI fill:#22c55e,color:#fff
    style ORCH fill:#f97316,color:#fff
    style GH fill:#0969da,color:#fff
```

## Further Reading

- [ADRs](ADR/README.md) — Architectural Decision Records
- [Hook Enforcement](hooks.md) — Multi-layer guardrail system
- [Review Workflow](../development/REVIEW_WORKFLOW.md) — Code review, rework cycles, exchange mechanisms
- [Guardrails & Safety](../design/guardrails.md) — Safety model and trust boundaries
