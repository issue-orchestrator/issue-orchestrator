# Issue-Orchestrator

Issue-Orchestrator is a control plane for AI-assisted software work. It takes GitHub issues, runs coding and review agents in isolated worktrees, and advances code only through the architecture, validation, review, recovery, and human-merge gates you define.

The issue runner is not the main point. The main point is the executable engineering contract around the work: a named architecture, module boundaries, guardrails, validation records, tests, review criteria, crash-safe state, and observable artifacts. Agents produce work; the orchestrator decides whether that work moves forward, goes back to rework, or needs a human.

> **Evaluator quick-start:** see **[EVALUATION.md](EVALUATION.md)** for a one-screen proof bundle (test count, ADRs, architecture enforcement, benchmark artifact path, authorship, and limitations).
>
> **Core thesis:** read [No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md), then [Making Agentic Development Sustainable](docs/design/sustainable-agentic-development.md).
>
> **Evaluating the engineering?** Read [Evaluating the System](docs/journeys/evaluating.md) for architecture decisions, quality signals, and where to read code.
>
> **Evaluating it as applied AI?** Start with [Applied AI Evaluation](docs/journeys/applied-ai.md), then run the [Portfolio Benchmark](docs/journeys/benchmarking.md) for a shareable proof bundle.

## What it does

Issue-Orchestrator turns GitHub issues into bounded, reviewable execution runs:

- Claims eligible GitHub issues and routes them to configured agent types.
- Creates an isolated git worktree per issue so agents can work concurrently without sharing dirty state.
- Launches coding, review, rework, or triage sessions through provider adapters.
- Requires agents to finish through structured `coding-done` / `reviewer-done` completion commands.
- Treats completion as untrusted input, then runs validation, review, reconciliation, and publish gates.
- Stores validation records keyed to the current commit so progress depends on the code that was actually checked.
- Runs reviewer agents and bounded rework loops before code is publish-ready.
- Uses GitHub labels and observed worktree state as crash-safe external truth.
- Surfaces timelines, structured events, validation artifacts, diagnostics, transcripts, and session replay for debugging.

```mermaid
flowchart LR
  ISS["GitHub issue"] --> WT["Isolated worktree"]
  WT --> CODE["Coding agent"]
  CODE --> DONE["Structured completion record"]
  DONE --> VAL{"Validation gate<br/>tests, lint, types, architecture"}
  VAL -->|fails| BLOCK["Blocked / retry / needs human"]
  VAL -->|passes| REV{"Review gate"}
  REV -->|changes requested| CODE
  REV -->|approved| PUB{"Publish gate"}
  PUB --> PR["Pull request"]
  PR --> YOU["Human merge"]
```

The default review exchange runs locally before PR creation. Draft-PR mode can create a draft PR earlier for GitHub-based review, but the authority is the same: no passing validation, no approved review, no publish-ready PR.

## Architecture and quality contract

Issue-Orchestrator does not magically know what "good" means for your codebase. You bring the architecture-centric artifacts; the orchestrator makes them hard to ignore.

- **Architecture:** This repo uses hexagonal architecture with ~31 Protocol ports in `src/issue_orchestrator/ports/`, adapters in `adapters/`, and dependency wiring in `entrypoints/bootstrap.py`.
- **Deep module boundaries:** Control, observation, domain, ports, adapters, execution, and UI contracts have distinct ownership. Decision logic is kept testable without GitHub, terminals, or storage implementations.
- **Guardrails:** AI hooks, git hooks, a `gh` wrapper, orchestrator validation gates, and CI all enforce different parts of the contract. Agents cannot rely on prompt compliance to bypass validation.
- **Tests and validation:** Validation commands can include unit, integration, and end-to-end tests, linting, typing, coverage gates, architecture checks, complexity checks, and repo-specific policy scans.
- **Ongoing test creation:** Agents can help draft tests, guardrails, coverage gates, ADRs, and failure triage summaries. Humans decide which standards deserve authority; once encoded, the workflow enforces them.
- **Documented decisions:** ADRs capture architectural choices that affect correctness, security, boundaries, and extensibility.
- **Observable proof:** Structured events drive the UI and tests; run artifacts, benchmark output, timelines, and session replay make agent work inspectable after the fact.

For the design rationale, see [No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md), [Guardrails & Safety Model](docs/design/guardrails.md), and the [architecture overview](docs/architecture/README.md).

## Design principles

AI agents are untrusted workers. They can be useful contributors, but they optimize for task completion unless the system enforces a stronger standard.

- **Mechanical enforcement over documentation** - AI hooks block unsafe commands before execution, git hooks validate before push, the orchestrator requires passing validation records before advancing state, and CI re-validates in a clean environment.
- **Agent intent, orchestrator authority** - Agents report what they did and what they want. The orchestrator validates that input and decides what happens next.
- **Observe-Plan-Apply loop** - Each tick gathers facts, decides actions, then applies changes through ports. Decision logic stays testable without I/O.
- **Labels as crash-safe truth** - GitHub labels persist outside the process, so restart recovery can reconstruct issue state after crashes or human edits.
- **Fail-fast by default** - Unexpected state should fail loudly instead of hiding bugs behind silent fallback behavior.

## Issue lifecycle

Each issue moves through a deterministic state machine. Labels and worktrees are observed before mutation, so the orchestrator can recover from crashes and reconcile human changes before advancing state.

```mermaid
stateDiagram-v2
  [*] --> Queued
  Queued --> Running : session launched
  Running --> Blocked : failed / needs human
  Blocked --> Running : retried / unblocked
  Running --> Validation : coding-done completed
  Validation --> Blocked : validation failed
  Validation --> Review : validation passed
  Review --> Rework : changes requested
  Rework --> Running : rework session launched
  Review --> Publish : approved
  Publish --> Ready : PR created / marked ready
  Ready --> [*] : human merges
```

## Dashboard

![Issue-Orchestrator dashboard with issue timeline](docs/images/dashboard-plus-timeline.png)

The dashboard gives you a live view of what the orchestrator is doing: issues flow through Queued, Running, Blocked, and Awaiting Merge columns. Click any issue to see its full timeline - review cycles, rework rounds, session recordings, and failure diagnostics.

![Issue timeline detail](docs/images/timeline.png)

Each issue's timeline shows the complete history: when code review started, what the reviewer found, how many rework cycles it took, and links to session recordings and transcripts.

![Session replay](docs/images/ui-session.png)

Session recordings let you see exactly what an agent did: terminal output rendered in an emulator replay. This is useful for debugging failures, auditing completion claims, and understanding why an issue moved to rework or needs-human.

Any client can connect: browser, VS Code ([MCP integration](docs/user/vscode.md)), or AI agents via the REST API.

## Guardrails

Agents cannot merge PRs. Humans merge. Validation runs automatically before code can advance, and it can include tests, linting, type checks, architecture checks, and repo-specific policy scans.

[Multi-layer hooks](docs/architecture/hooks.md) enforce these rules at the AI-agent level, git level, orchestrator level, and CI. The guardrails are installed and verified, not just described. See [Guardrails & Safety Model](docs/design/guardrails.md) for the guarantee and limitation boundaries.

## Proof for evaluators

If you're evaluating this as serious applied-AI engineering, start with the proof path rather than the feature list:

- **[EVALUATION.md](EVALUATION.md)** - One-screen evidence bundle: test count, ADRs, architecture enforcement, benchmark artifact path, authorship, and limitations.
- **[No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md)** - The operating model: agents contribute inside constraints, humans decide which constraints deserve authority.
- **[Applied AI Evaluation](docs/journeys/applied-ai.md)** - How to frame the project without overselling autonomy.
- **[Portfolio Benchmarking](docs/journeys/benchmarking.md)** - Deterministic simulated scenarios with markdown, JSON, JUnit, and pytest-output artifacts.
- **[Async E2E Runner](docs/user/e2e.md)** - Live system proof with persistent run history, signal scoring, quarantine support, and dashboard visibility.

## Quickstart

```bash
make venv                              # creates .venv with uv + correct Python
source .venv/bin/activate
cd /path/to/your/project               # run setup/start in the repo you want to automate
export ISSUE_ORCH_GITHUB_TOKEN=ghp_...
issue-orchestrator setup
issue-orchestrator setup-guardrails    # if you skipped the wizard prompt
issue-orchestrator init
# review, commit, and push the generated onboarding files (or set worktrees.seed_ref: HEAD)
issue-orchestrator doctor
issue-orchestrator start
```

Run the setup/start commands from the target repo, not from the `issue-orchestrator` checkout. Before `start`, commit and push the generated onboarding files to the worktree seed ref (by default `origin/<default-branch>`), or set `worktrees.seed_ref: HEAD` if you're doing local-only evaluation. You'll also need a supported AI coding CLI installed. See [Installation](docs/user/installation.md) and [Quickstart Guide](docs/user/quickstart.md) for detailed setup, prerequisites, and configuration.

If you want your AI assistant to drive the setup for you, use the [Agent-Guided Onboarding](docs/journeys/agent-guided-onboarding.md) path.

## More

**Async E2E Test Runner** - Background test execution with progress tracking, resumable runs, flake detection, quarantine support, and signal scoring. Survives orchestrator restarts. See [E2E documentation](docs/user/e2e.md).

**Goal Pilot** *(planned)* - A designed-but-not-yet-implemented agentic layer that would take high-level goals and break them into orchestrator actions, constrained by the same safety guarantees as the core. See [user guide](docs/user/goal_pilot.md) and [design document](docs/design/goal-pilot.md).

## Who it's for

- Solo builders and small teams using coding agents on real repos
- Teams willing to encode architecture, validation, and review standards as enforceable project contracts
- People who want strong safety and guardrails: humans merge, verification gates, reconciliation, and inspectable artifacts

## Project status

**Beta** - Core orchestration, guardrails, review workflow, and the web dashboard are stable and in daily use. The E2E test runner is newer and still maturing. Goal Pilot is a planned feature, not yet implemented. APIs may change.

~100K lines of Python, 5,600+ automated tests including 5,200+ unit tests, and 24 architecture decision records. For a one-screen evaluator summary, see [EVALUATION.md](EVALUATION.md).

For guidance on what is stable and where to read code, see [Evaluating the System](docs/journeys/evaluating.md).

## Documentation

Pick the path that fits:

- **[Getting Started](docs/journeys/getting-started.md)** - Install, configure, run your first issue
- **[Agent-Guided Onboarding](docs/journeys/agent-guided-onboarding.md)** - Let an AI assistant drive setup and first-run validation
- **[No Free Lunch for Coding Agents](docs/journeys/no-free-lunch.md)** - Why the engineering contract matters more than the issue runner
- **[Applied AI Evaluation](docs/journeys/applied-ai.md)** - How to present and evaluate the system as serious applied-AI engineering
- **[Portfolio Benchmarking](docs/journeys/benchmarking.md)** - Generate a benchmark artifact bundle from deterministic scenario coverage
- **[Evaluating the System](docs/journeys/evaluating.md)** - Architecture, guardrails, quality signals, where to read code
- **[Developing](docs/journeys/developing.md)** - Dev setup, conventions, testing, how to make changes

Reference docs:

- **User:** [Installation](docs/user/installation.md) · [Tutorial](docs/user/tutorial.md) · [Configuration](docs/user/configuration.md) · [Configuration Reference](docs/user/configuration_reference.md) · [FAQ](docs/user/faq.md)
- **Architecture:** [Overview](docs/architecture/README.md) · [ADRs](docs/architecture/ADR/README.md) · [Guardrails](docs/design/guardrails.md) · [Hooks](docs/architecture/hooks.md)
- **Development:** [Testing](docs/development/TESTING.md) · [Creating Guardrails](docs/development/CREATE_GUARDRAILS.md) · [Troubleshooting](docs/development/TROUBLESHOOTING.md) · [Review Workflow](docs/development/REVIEW_WORKFLOW.md)
- **Features:** [E2E Runner](docs/user/e2e.md) · [Goal Pilot](docs/user/goal_pilot.md) · [VS Code](docs/user/vscode.md)
