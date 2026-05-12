# ADR 0028: Deep module boundaries

**Status:** Proposed
**Date:** 2026-05-05
**Milestone:** M5
**Tracks:** Issues #6226, #6228, #6229, #6230, #6231, #6232, #6233, #6235, #6236, #6237, #6238, #6262

## TL;DR

If you are acting on this convention day-to-day, these five invariants are the substance:

1. **A module is a Python package.** Its `__init__.py` re-exports the public interface and only the public interface. Internals stay package-local; this is mechanically enforced by `import-linter`, not by convention (decisions 2, 5, 13).
2. **A package is worth creating only when there is meaningful complexity to hide** behind a narrow interface. Value objects, single-function utilities, and ≤5-line shims are not modules. The deep-module test in decision 1 is the filter.
3. **Hard-delete what's replaced.** No deprecation windows; mypy/pyright and tests catch missed callers (decision 8).
4. **Test in two tiers where complexity warrants.** Facade-tier tests for public behavior; inner-tier tests for individual internals when failure localization matters more than the test cost (decision 7).
5. **Forward-fix is the intended recovery model** (decisions 10 and 12), but it is **operationally deferred** until the matrix self-test in #6320 lands and proves the matrix is a real gate. Until then, regressions surface through manual review.

Everything below is rationale, parameters, or detail.

## Context

The orchestrator's `control/` directory has accumulated ~80 flat Python files, including eleven `completion_*.py` helpers, fourteen `session_*.py` helpers, and several `publish_*.py` and `worktree_*.py` clusters. The PTY/terminal layer is split across three execution paths (`agent_runner.py`, `persistent_round_runner.py`, `interactive_round.py`) plus cleaning and prettification utilities scattered into `infra/`. Several ports duplicate one concept across multiple tips (`TimelineReader`/`TimelineWriter`/`TimelineStore`; `LabelSet`/`LabelStore`; `WorktreeManager`/`WorktreePolicy`).

Recurring bug clusters in 2026 (#6069, #6082, #6083, #6074, #6097, #6121, plus the TUI-cleaning churn in commits `48d2664`, `70ad9e9`, `417cc9a`, `138296c`, `c960a4c`, `56118c0`, `052f258`, `b7115a9`, `9f66ad7`) all reduce to one root cause: there is no single owner of the relevant workflow concept, so each fix lands in whichever helper happened to be in front of the engineer. The problem is structural, not behavioral.

This is the fragmentation Ousterhout describes as "shallow modules": interfaces nearly as complex as their implementations, callers needing to compose multiple modules to express one logical thing. The fix is **deep modules** — narrow public interfaces hiding the complexity callers should not have to know about.

The M5 milestone re-draws these boundaries. This ADR codifies the conventions every M5 PR follows and that future architectural work should adopt by default.

## Decision

### 1. Deep modules are the unit of architecture

Per Ousterhout: a module is *deep* when its interface is significantly smaller than its implementation. The Unix file system (`open` / `read` / `write` / `lseek` / `close`) hiding disk allocation, caching, permissions, and concurrent access is the canonical example. The orchestrator's existing `EventSink` (one method `publish`, hides pluggy plugin manager + locking + SSE fan-out) is a deep module that already works.

Every module created or revised under M5 must pass the deep-module test: **a caller can express what it wants without knowing how the module accomplishes it.** Where this fails today, M5 redraws the boundary.

**What qualifies as a primary module.** The deep-module test has positive and negative cases. Treat these as a filter before reflexively packaging every concept:

Yes:
- A 3-method port that hides SQLite, scope handling, and recording cadence (`Timeline`) — interface is a fraction of the implementation; deep.
- A facade composing 11 internal helpers behind one `process(session, status)` method (`CompletionPipeline`) — large iceberg, small tip; deep.
- A port hiding pexpect, raw `pty.openpty()`, cleaning, and provider parsing (`TerminalSession`) — multi-concern complexity hidden behind 5 verbs; deep.

No:
- A value object or dataclass with no behavior. Make it a domain type, not a module.
- A single utility function or a ≤5-line shim. Inline it or place it in an existing module.
- A pass-through re-export pile — a package whose `__init__.py` exposes everything inside. That is not hiding anything; it is just a directory with the cost of a package and none of the benefit.
- A folder created to give a sub-feature its own home when the sub-feature's complexity is genuinely shared with its parent. Internals stay internal (see decisions 5 and 6).

Test to apply before creating a new package: **articulate what the package hides.** If the answer is "nothing meaningful," it is not a deep module — do not package it.

### 2. Module = Python package

Every primary module is a Python package — a directory with `__init__.py` — regardless of how small the implementation currently is. The `__init__.py` re-exports the public interface and **only** the public interface.

```python
# src/issue_orchestrator/ports/timeline/__init__.py
from .timeline import Timeline
__all__ = ["Timeline"]
```

Rationale: a 3-method port that hides SQLite, scope handling, and recording cadence is genuinely deep even if its implementation fits in one file. Treating it as a package makes the "this is a module, not a file" boundary structural rather than conventional, eliminates the file→package migration when internals later emerge, and lets `import-linter` rules apply uniformly.

### 3. Module, file, and class naming

| Slot | Rule | Examples |
|---|---|---|
| Package directory | `snake_case`, name describes the module's concept | `timeline`, `completion`, `terminal_session` |
| Outer-facade file | Same as the package, OR concept-fit when the package name is broad | `timeline.py`, `pipeline.py`, `session.py` |
| Outer class | `CamelCase`. No `Impl` / `Facade` / `Service` suffix unless meaningful | `Timeline`, `CompletionPipeline`, `TerminalSession` |
| Behavioral suffix on the class | Allowed where it clarifies role (`Pipeline`, `Workflow`, `Manager`, `Acquisition`); not required | `CompletionPipeline`, `PublishWorkflow` |

Caller imports always read: `from issue_orchestrator.<layer>.<package> import ClassName`. No deeper paths.

### 4. Adapter naming: backing-technology prefix

Concrete adapter classes that implement a port carry the **backing technology as a prefix**, not a `Default` placeholder. This is self-documenting and matches the existing convention (`PluggyEventSink`, `JsonSessionStore`).

| Port | Adapter |
|---|---|
| `Timeline` | `SqliteTimeline` |
| `IssueLabelManager` | `GitHubIssueLabelManager` |
| `WorktreeAcquisition` | `GitWorktreeAcquisition` |
| `GoalPilotSession` | `SqliteGoalPilotSession` |
| `QueueCache` | `JsonQueueCache` |
| `ValidationArtifacts` | `FileSystemValidationArtifacts` |
| `SessionRunStorage` | `FileSystemSessionRunStorage` |
| `SessionMetadata` | `FileSystemSessionMetadata` |
| `TerminalSession` | `LocalTerminalSession` |

When there is no meaningful backing-tech distinction (rare), `Default<Port>` is the fallback. Adapters live in `execution/<package>/` mirroring the port's package layout.

### 5. Internal modules and private helpers

A module's package may contain three kinds of inhabitants:

- **The outer facade** — re-exported by `__init__.py`. The only thing external callers ever import.
- **Internal modules** — sibling files inside the package (no underscore prefix). They may have their own narrow interfaces and their own inner-tier tests, but they are *not importable from outside the package*. Used by the facade and possibly by other internal modules within the same package.
- **Private helpers** — sibling files prefixed with `_` (e.g., `_record_parsing.py`). Used only by other files within the same package; tested transitively through the internal that uses them.

Three rules anyone can apply at a glance:

1. Directory under `ports/`, `execution/`, or `control/` with `__init__.py` → primary module. Treat the `__init__.py` as a contract.
2. Sibling file inside such a package, no `_` prefix → internal module.
3. Sibling file with `_` prefix → private helper.

### 6. Law of Demeter for external access

When an external caller needs a capability that an internal module provides, the response is to **add the capability to the parent module's public interface**, not to expose the internal. The parent then delegates to its internal; the caller never reaches in.

```python
# Wrong: caller reaches into the internal
from issue_orchestrator.control.completion.review_exchange import ReviewExchangeOrchestration
included = ReviewExchangeOrchestration(...).is_present_for(session)

# Right: parent exposes the capability
from issue_orchestrator.control.completion import CompletionPipeline
included = pipeline.completion_included_review_exchange(session)
```

Rationale: every promotion-by-demand widens the system's public interface and shallows the deep module. Adding one method to the parent keeps the iceberg intact. Promotion of internals to primary modules is **deferred**: when a concrete case arises where multiple unrelated parents need the same capability and the parent's interface is bloating with thin pass-through methods, that is the moment to write promotion criteria — not before.

### 7. Two-tier testing

Each deep module has two test tiers:

- **Inner tier** — tests of individual internal modules. White-box-ish; allowed to know the internal exists. Located at `tests/unit/<module>/test_<internal>.py`. Updated when internals reshape; that's expected, not a regression.
- **Facade tier** — scenario tests of the deep module's public contract. Black-box; do not import internals. Located at `tests/unit/<module>/test_<facade>.py`. Updated only when the public contract changes.

Both tiers are required. Inner-tier tests give fast failure localization; facade-tier tests give contract stability. Either alone is worse than both.

The `tests/unit/` directory mirrors the source package layout. `tests/unit/<module>/__init__.py` is empty. Tests are exempt from `import-linter` package-sealing contracts via the `source_modules` scoping (production code only); inner-tier tests can freely import internals.

### 8. Hard deletion of replaced modules

When an old port or class is replaced, it is **deleted in the same PR** that introduces the replacement. No deprecation window, no shim re-exports, no `DeprecationWarning`. Type checking (mypy/pyright) catches missed callers in CI before runtime; existing test coverage catches behavioral regressions.

Rationale: deprecation windows in internal codebases tend to become permanent. The clean break has lower cumulative cost.

### 9. Touched = migrated, separate commits

When a PR has to touch a flat module that conceptually belongs in a deep-module package (e.g., a stray `control/foo.py` that should live inside `control/completion/`), the PR migrates it as part of the work. The migration goes in a **separate commit** within the PR so reviewers can isolate it from the substantive change.

Rationale: opportunistic migration prevents long-term inconsistency; separate commits prevent scope creep from making PRs unreviewable. The cost of leaving inconsistency is recurring confusion; the cost of migrating is one commit per touched flat module.

### 10. Cross-agent test matrix scope

For modules that touch agent-runtime behavior (#6230, #6235, #6236, #6237, #6262), the cross-agent test matrix runs against every supported provider (Claude Code TUI, Claude Code stream-json, Codex). The matrix gates merge.

The matrix runs **on every PR that changes any code path**. The only skip condition is when a PR's diff is purely documentation — `docs/**`, `**/*.md`, `LICENSE`, `.gitignore`, GitHub issue templates — with one **deferred caveat** below.

Rationale: path-pattern selection of code-relevant tests systematically misses indirect impacts (shared types, configuration, transitive imports, shared fixtures). The first regression that slips through path-pattern filtering destroys trust in the filter; running everything on code changes preserves it. Doc-only skips are unambiguously safe.

**Deferred — convention-ADR changes do not currently trigger the matrix.** Material changes to architecture-convention ADRs (this one and any future ADR that prescribes enforcement) ideally should run the validation jobs, since changing the convention is itself a code-affecting decision. The current `.github/workflows/validate.yml` path filter does not include `docs/architecture/ADR/**` in the validation-relevant paths, so this ADR's own PR was permitted to skip `validate-fast` / `validate-agent`. Reconciling that gap is tracked in #6321 (workflow filter update). Until #6321 lands, convention-ADR changes are reviewed by judgment, not by the gate.

### 11. Performance regression budget on hot paths

Refactors that touch hot paths must demonstrate **≤10% slowdown** on a fixed scenario, measured on the PR branch versus `main`, averaged across at least three runs. Hot paths in M5 are: orchestrator tick rate (#6228 label sync), validation cycle (#6235), session launch latency (#6237), PTY throughput (#6262).

Non-hot-path refactors do not require measurement. Behavior preservation is verified by tests; performance preservation is verified only where regressions would cumulatively matter.

### 12. Forward-fix discipline

When a refactor lands and surfaces a P1 in production, the response is **forward-fix**, not revert. The cross-agent matrix and inner-tier tests are the gate that makes forward-fix safe; if they failed to catch the regression, the test gap is the bug to fix alongside the symptom.

Rationale: this codebase ships autonomously, and revert workflows in autonomous systems are operationally complex. The matrix and two-tier testing exist precisely so forward-fix is the cheap path. Decisions 10 and 12 lock together — without (10), (12) becomes "we discover regressions in production."

### 13. Mechanical enforcement

Each primary module's package boundary is enforced by an `import-linter` contract:

```toml
[[tool.importlinter.contracts]]
name = "<module> internals are private"
type = "forbidden"
source_modules = ["issue_orchestrator"]
forbidden_modules = ["issue_orchestrator.<layer>.<module>.**"]
ignore_imports = [
    # The package itself (i.e. its __init__.py) may import any internal — required for re-export.
    "issue_orchestrator.<layer>.<module> -> issue_orchestrator.<layer>.<module>.**",
    # Internals may import other internals at any depth — required for intra-package collaboration.
    "issue_orchestrator.<layer>.<module>.** -> issue_orchestrator.<layer>.<module>.**",
]
```

Reads as: "no production module may import any descendant of `<module>`, **except** (a) `<module>` itself can import its descendants — needed by `__init__.py` to re-export the public surface — and (b) descendants can import other descendants — needed for internal collaboration inside the package." Concretely, `issue_orchestrator.control.completion.rounds → issue_orchestrator.control.completion.records` is allowed by exception (b); `issue_orchestrator.entrypoints.cli → issue_orchestrator.control.completion.rounds` is forbidden because the source is outside the package.

Tests are exempt because `source_modules` excludes the `tests/` tree. The contract for each module lands in the same PR that creates the module.

`make lint-arch` invokes `import-linter` (alongside the existing arch checks) and fails the build on contract violations. CI reports the offending caller and the missed boundary by name.

**Contract-behavior tests.** A contract that's syntactically valid but semantically wrong passes silently — a contract that forbids what no caller happens to attempt is indistinguishable from a correct one until someone tries the legitimate import that the contract has accidentally banned (the bug that landed in this ADR's first draft). Every `import-linter` contract therefore ships alongside a paired test fixture at `tests/architecture/test_<module>_package_seal.py` exercising both directions:

- **Negative case** — an import from outside the package is rejected ("would-be external caller cannot reach an internal").
- **Positive case (intra-package)** — an internal can import another internal ("collaborators inside the package are allowed").
- **Positive case (re-export)** — the package root can import an internal ("`__init__.py` can re-export").

Contract-behavior tests run under `make lint-arch` alongside the live `import-linter` invocation. Without them, contract correctness depends on accident.

**Public-API drift test.** A single AST-based test (lands in #6226 alongside the tooling setup) walks every primary-module package and asserts that the package's `__init__.py` re-exports only documented public names — nothing prefixed with `_`, nothing missing from `__all__` if `__all__` is defined, no wildcard re-exports leaking internals. This catches the slow erosion of the public surface that's hardest to spot in PR review.

**Matrix self-test.** The cross-agent matrix (decision 10) is the highest-leverage guardrail in this milestone, and like the import-linter contracts it can fail silently if its scaffolding is broken. A matrix that always passes is indistinguishable from a matrix that's working until someone introduces a regression. The mitigation is a periodic self-test that injects a known behavioral regression and asserts the matrix catches it. The matrix self-test is **a precondition for the Q12 forward-fix discipline** to be operationally safe; until the self-test exists and passes, forward-fix is not yet viable as a recovery model and the TL;DR's decision-5 framing should be read as aspirational.

The matrix self-test is tracked in **#6320**, scoped to M5, owned by `agent:backend`. Acceptance criteria: a deliberate behavioral break is injected into a deep-module internal, the matrix is run, at least one provider's matrix run fails on the injected regression, the injection is reverted automatically, and the self-test runs on a weekly cron and on demand.

### 14. Per-module ADRs reference this one

Each M5 PR ships a short companion ADR (numbered 0029 onward) documenting:

- The module's outer facade name and public methods.
- The internal modules and what each is responsible for.
- The old code that was deleted.
- The `import-linter` contract added.

Per-module ADRs do not restate the rationale here — they reference ADR-0028 and document the specifics of *this* module. Length: typically half a page.

### 15. Reviewer process

PRs in this milestone are reviewed in two stages: `agent:script-review` first, human ratification before merge. Script-review's approval is necessary but not sufficient; only the human gate merges.

The repository workflow that applies this convention — the `agent:script-review` label semantics and the corresponding `AGENTS.md` (a.k.a. `CLAUDE.md` — the latter is a symlink to the former) guidance — is **not yet in place at the time of this ADR**. It lands as part of #6226, the pattern-establishing PR. Until that lands, the convention is applied by manual reviewer judgment.

This is the only deferred piece of the convention; it is deferred because adding workflow documentation is in scope for #6226, not for this docs-only ADR.

### 16. Deferred decisions

Two areas are intentionally not codified by this ADR:

- **Trace events at module boundaries** are not added as a first cut. Where the new boundaries map onto existing events (e.g., `SESSION_STARTED`), reuse them. New `EventName` entries land only when a real observability gap exists, not as ceremony at every facade method.
- **Promotion criteria** for internal modules are not pre-defined. The Law of Demeter (decision 6) is the durable guidance that prevents premature promotion; concrete criteria are written when the first genuine case arises.

## Consequences

### Positive

- Module count drops materially. ~80 flat files in `control/` collapse into a small set of packages with internal structure. Bootstrap wiring shrinks from ~40 fields toward ~15.
- Test failures localize. Inner-tier tests point at the broken collaborator; facade-tier tests confirm the contract.
- Boundary erosion becomes a CI failure, not a code-review judgment call. The first time someone tries to import an internal from outside its package, the build fails and names the violation.
- Naming and structure self-document. A new contributor reading `control/completion/` knows immediately that `completion` is a deep module and `__init__.py` is its contract.
- The same convention scales to future architectural work; M5 is the milestone that establishes it, not the only milestone that benefits.

### Negative

- One-time migration cost: every M5 issue carries some structural work in addition to its substantive change.
- Single-line `__init__.py` files for the smaller packages add ~10 files of boilerplate.
- Per-module ADRs add minor documentation work on every PR.
- `import-linter` contract authoring per package is a repeating task across the milestone.

### Risks

- A deep-module boundary is mis-drawn the first time. Mitigation: per-module ADRs document the boundary for review; the convention permits future ADRs to amend a boundary if experience reveals a better cut.
- Inner-tier tests are coupled to internal structure and need updating when internals reshape. Mitigation: this cost is explicit and accepted; it is paid by the same engineer who makes the reshaping change, when context is fresh.
- The cross-agent matrix slows CI on PRs that change code (no longer just on PRs that change "matrix-relevant" code). Mitigation: this is a deliberate trade for catching indirect-impact regressions at PR time rather than nightly.
- Forward-fix discipline (decision 12) could be misused as "ship now, fix later." Mitigation: forward-fix only applies post-merge; pre-merge gates remain. The matrix and two-tier testing are non-negotiable PR gates.

## Enforcement

- Per-package `import-linter` contracts (decision 13) — landed in the PR that creates the module.
- **Contract-behavior tests** (decision 13) — paired with each contract, verifying it permits and rejects what it claims, run on every PR.
- **Public-API drift test** (decision 13) — AST-based, lands in #6226, catches accidental widening of `__init__.py` re-exports.
- **Matrix self-test** (decision 13, tracked in #6320) — periodic injection of a known regression to verify the cross-agent matrix is a real gate. **Precondition for Q12 forward-fix discipline; forward-fix is operationally deferred until #6320 lands.**
- `make lint-arch` invokes `import-linter` and fails the build on contract violations.
- mypy/pyright catch missed callers when an old type is hard-deleted (decision 8).
- Cross-agent matrix gates merge for runtime-touching modules (decision 10).
- Performance regression budget on hot paths (decision 11) is documented in PR descriptions.
- Per-module ADRs (decision 14) are part of acceptance criteria for every M5 PR.
- The two-stage review process (decision 15) is applied manually until #6226 introduces the documented label workflow in `AGENTS.md` (`CLAUDE.md` is a symlink to it).

## References

- Ousterhout, John. *A Philosophy of Software Design*, 2nd ed. (2021). Chapters 4 ("Modules Should Be Deep") and 5 ("Information Hiding and Leakage").
- Parnas, David. "On the Criteria To Be Used in Decomposing Systems into Modules." *Communications of the ACM*, 1972.
- Cockburn, Alistair. "Hexagonal Architecture." 2005. See also ADR-0011.
- Lieberherr, Karl, and Ian Holland. "Assuring Good Style for Object-Oriented Programs." *IEEE Software*, 1989. (Law of Demeter.)
- Go's `internal/` package convention: https://go.dev/doc/go1.4#internalpackages — the directly comparable language-enforced analog of decision 13.
- Related ADRs: 0011 (hexagonal architecture), 0012 (mechanical guardrails), 0014 (observe-plan-apply loop), 0017 (orchestrator coordinates, planner decides), 0018 (worktree isolation).
- M5 issue tracking: #6226 (pattern-establishing PR; lands `import-linter` setup and the first contract), #6228, #6229, #6230, #6231, #6232, #6233, #6235, #6236, #6237, #6238, #6262.
- M5 guardrail follow-ups: #6320 (matrix self-test scaffolding — precondition for Q12), #6321 (CI workflow filter for architecture-ADR changes — referenced by decision 10).
