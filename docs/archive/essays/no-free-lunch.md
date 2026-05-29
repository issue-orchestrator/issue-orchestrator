# No Free Lunch for Coding Agents

Archived copy of the former public thesis essay. The active public narrative is now [A Software Engineering Control Plane for Agentic Development](../../journeys/software-engineering-control-plane.md).

Issue-Orchestrator is a control plane for AI-assisted software work. It does not make coding agents magically trustworthy, discover your engineering standards, or make those standards explicit. It enforces the checks, workflow rules, and review boundaries you configure around agent work.

AI agents are good at finishing bounded tasks. They are not automatically good at maintaining large systems. Left alone, they can pass the narrow task while weakening architecture, skipping validation, increasing complexity, or producing work that is hard to review.

Issue-Orchestrator is built around a more conservative premise:

> Agents can produce work. The system decides whether that work moves forward, goes to rework, or needs a human.

## The Short Version

Issue-Orchestrator runs coding agents on GitHub issues in isolated worktrees, then controls progress through validation, review, recovery, and human merge authority.

The issue runner is not the main point. The main point is the engineering contract around the work.

That contract has three layers:

1. Work shape: milestones, human-sized GitHub issues, dependencies, labels, and reviewable pull requests.
2. Quality standard: architecture boundaries, tests, coverage, linting, type checks, review criteria, and CI.
3. Operational control: isolated worktrees, validation records, bounded review/rework, crash recovery, transcripts, artifacts, and human merge authority.

The gates are checkpoints. The contract is the product.

## What You Bring to the Table

Issue-Orchestrator is strongest when the project brings architecture-centric artifacts that can be evaluated mechanically:

- a named architecture with visible module boundaries
- guardrails that encode dependency direction, forbidden side effects, and completion rules
- tests that cover core behavior, integration boundaries, and user-facing workflows
- validation commands that combine tests, linting, typing, coverage, and architecture checks
- ADRs or other design records that explain why the boundaries exist
- ongoing test and guardrail creation as the system learns from failures

Agents can help draft these artifacts, but they should not be the authority for them. Humans choose the architecture and quality bar; the orchestrator enforces the checks that make those choices operational.

## Work Shape

Before enforcement, work has to be shaped.

Large goals need to become milestones. Milestones need to become human-sized GitHub issues. Issues need acceptance criteria, labels, dependencies, and enough context for an agent to attempt the work without inventing the project plan on the fly.

This matters because agents perform best on bounded work. A vague, multi-week objective tends to produce brittle execution and difficult review. A small issue with a clear expected outcome can become an isolated run, a focused diff, a reviewable PR, and a recoverable state transition.

Issue-Orchestrator does not decompose your project for you. It makes decomposition visible, durable, routable, and operational:

- GitHub issues hold intent.
- Milestones group work into project phases.
- Labels route work to agent types and encode state.
- Dependencies and blocked states make sequencing explicit.
- Pull requests turn completed work into human-reviewable units.

This is not a replacement for product or engineering judgment. It is a way to make that judgment external and durable enough for agents to work against.

## Quality Standard

Issue-Orchestrator does not know your architecture.

It does not know how your system should be tested. It does not know your acceptable code complexity, where abstraction helps, what coverage threshold is meaningful, or which architectural boundaries matter.

Those standards have to come from the project.

In one project, the architecture might be hexagonal ports and adapters. In another, it might be package boundaries, service boundaries, dependency direction, UI/domain separation, or something else entirely. The important thing is not that every project chooses the same architecture. The important thing is that the project declares the architecture it wants and encodes checks that can detect drift.

The same applies to validation. Tests alone are not always enough. A serious validation contract may include:

- unit tests
- integration tests
- end-to-end tests
- coverage gates
- linting
- type checks
- architecture checks
- complexity checks
- pre-push hooks
- CI and branch protection

Issue-Orchestrator can enforce a validation command, require passing records on the current commit, preserve those records as artifacts, and refuse to advance work that does not satisfy the contract.

It cannot decide what the contract should be.

## Operational Control

Once work is shaped and standards are encoded, Issue-Orchestrator controls how agent work moves.

Agents run in isolated worktrees. They complete work through structured commands. They do not get to decide that validation can be skipped. They do not get to merge. Reviewer agents can approve or request changes, and rework loops are bounded so the system can escalate instead of spinning indefinitely.

The system assumes failure is normal:

- processes can crash
- labels can drift
- humans can change state
- main branches can move
- providers can fail
- agents can get stuck
- validation can expose weak work

Issue-Orchestrator treats those cases as first-class workflow states. Labels, timelines, transcripts, validation records, and artifacts make failures easier to review. Recovery and reconciliation happen before the system mutates external state.

The goal is not pretend autonomy. The goal is controlled autonomy inside explicit engineering constraints.

## No Free Lunch

There is no free lunch.

Issue-Orchestrator does not know what "good" means for your system. It cannot infer the right architecture. It cannot prove that your abstractions are elegant. It cannot guarantee that your issues are well-scoped. It cannot replace the judgment required to maintain a large system.

What it can do is make your chosen standards hard to ignore.

If architecture boundaries are encoded, violating them stops progress. If coverage gates are configured, weak validation blocks publish. If reviewer approval is required, agents do not declare success unilaterally. If work is too ambiguous, the system can surface a blocked or needs-human state rather than pretending the run succeeded.

Humans still own the standard.

## Where Agents Can Help

The useful twist is that agents can help create the discipline.

They can help draft:

- architecture proposals
- architecture checks
- tests
- coverage gates
- linters and static checks
- ADRs
- milestones
- scoped GitHub issues
- reviewer prompts
- failure triage summaries

But that help needs a human owner. Agents can propose the standard; humans decide whether it is good enough to enforce.

That is the core operating model: agents contribute inside constraints, and humans decide which constraints deserve authority.

## Why This Matters

Agentic development fails when it optimizes for task completion while the system quietly loses structure.

Issue-Orchestrator is an attempt to make the opposite workflow practical: agents can work in parallel, but the project remains bounded, validated, reviewable, recoverable, and under human control.

It is not a prompt collection. It is not a claim that agents can safely ship code on their own. It is a reliability layer for using agents on real software projects without giving up engineering discipline.

Sustainable agentic development requires structure, validation, reviewability, and human oversight.

Issue-Orchestrator turns those requirements into an executable workflow.
