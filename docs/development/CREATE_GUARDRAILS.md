# Preparing Your Codebase for AI Development

AI agents follow rules that are enforced, not rules that are documented. This guide helps you set up mechanical enforcement for your codebase.

## Why This Matters

AI agents are productive when constraints are clear and mechanically enforced. Without guardrails:

- Agents find creative workarounds to skip validation
- Code quality degrades silently over time
- Architectural boundaries erode
- Debugging agent mistakes becomes expensive

Quality comes from structure and enforcement, not documentation and hope.

## Philosophy

These principles should guide your guardrail design:

- **Mechanical enforcement over documentation** - If it's not enforced, it won't happen
- **Fail loudly over silent degradation** - Agents need clear signals when they violate rules
- **Defense in depth** - Multiple layers catch different failure modes
- **Fast feedback loops** - Agents should know quickly when they've made a mistake
- **Assume agents will test boundaries** - Not maliciously, but creatively

## Step 1: Analyze Your Project

Before creating guardrails, understand what you're working with:

- **Language(s) and build system** - What enforcement tools exist for this ecosystem?
- **CI/CD platform** - Where will guardrails run? GitHub Actions, GitLab CI, Jenkins?
- **AI agent tooling** - Claude Code, Codex, Cursor? Do they support hooks or constraints?
- **Current architectural patterns** - Is there an intended architecture? Hexagonal, layered, modular?
- **Legacy state** - How much existing code? How much technical debt?
- **What has gone wrong before?** - Past bugs, violations, or incidents point to needed guardrails

## Step 2: Assess Architectural State

Determine where your project falls:

| State | Description | Implication |
|-------|-------------|-------------|
| **Greenfield** | No existing code | Can design structure to reflect architecture from the start |
| **Structured** | Clear boundaries reflected in directory structure | Ready for architectural guardrails |
| **Mixed** | Some structure, some legacy areas | May need hybrid approach |
| **Legacy/Spaghetti** | No clear boundaries in structure | Need to choose: restructure, tooling-only, or freeze baseline |

**Key question:** Does the directory structure reflect the intended architecture?

If yes, you can write simple path-based rules ("code in `domain/` cannot import from `adapters/`").

If no, you'll need tooling that works at the code level rather than the file system level, or you'll need to restructure.

## Step 3: Understand the Options

### Option A: Structure Reflects Architecture (Strongest Enforcement)

Directory structure mirrors architectural boundaries.

**How it works:**
- Directories named after architectural concepts (`domain/`, `ports/`, `adapters/`, `infrastructure/`)
- Import/dependency rules expressed as path-based constraints
- Language visibility features reinforce boundaries (package-private in Java, `__all__` in Python)

**Benefits:**
- Self-documenting - architecture visible from file tree ("screaming architecture")
- Language-level enforcement in addition to tooling
- Simplest rules to write and understand
- Easier microservices extraction later

**Tradeoffs:**
- Requires restructuring if not already organized
- Restructuring can be disruptive to active development

**When to choose:**
- Greenfield projects (do this from the start)
- Projects where restructuring is feasible
- When you want the strongest possible enforcement

### Option B: Tooling-Only Enforcement (Viable Without Restructuring)

Define layers/boundaries in configuration, enforce with tools regardless of physical structure.

**How it works:**
- Tools like ArchUnit (Java), import-linter (Python) can define logical groupings
- Rules reference modules/classes/annotations rather than paths
- Layers can be individual files, not just directories

**Benefits:**
- Works without restructuring
- Can adopt incrementally
- Flexible rule definitions

**Tradeoffs:**
- Rules are more complex to write and maintain
- No language-level enforcement (everything may need to be public)
- Architecture not visible from file tree
- Logical groupings must be maintained in configuration

**When to choose:**
- Restructuring isn't feasible right now
- Architecture is well-defined but not reflected in structure
- You need guardrails quickly without major changes

### Option C: Freeze Baseline + Gradual Migration (Pragmatic for Legacy)

Capture existing violations as an accepted baseline, enforce rules only on new/changed code.

**How it works:**
- Tools like ArchUnit's `FreezingArchRule` record all current violations
- Subsequent runs only fail on *new* violations
- As you fix violations, they're removed from the baseline
- Prevents regression while allowing incremental improvement

**Benefits:**
- Can adopt guardrails immediately without fixing everything first
- Prevents new violations while acknowledging existing debt
- Gradual improvement path
- Team isn't blocked by legacy issues

**Tradeoffs:**
- Technical debt is explicitly accepted (at least temporarily)
- Baseline files must be maintained and version-controlled
- Risk of baseline becoming permanent if not actively reduced

**When to choose:**
- Large legacy codebase with many existing violations
- Need to prevent regression while planning larger refactoring
- Team can commit to gradually reducing baseline over time

### Option D: Code Quality Only (Minimum Viable)

Skip architectural guardrails entirely, focus on universal code quality checks.

**How it works:**
- Type checking (language-appropriate)
- Linting rules
- Complexity limits
- Automated formatting

**Benefits:**
- Valuable regardless of architecture
- No restructuring required
- Tools exist for every language
- Quick to set up

**Tradeoffs:**
- Doesn't enforce architectural boundaries
- Won't prevent coupling or dependency violations
- May not be enough for complex codebases

**When to choose:**
- Architecture isn't well-defined yet
- Small project where boundaries don't matter much
- First step before more sophisticated guardrails
- Resources don't allow for more comprehensive approach

## Step 4: Choose an Approach

Based on your analysis, recommend an approach. Consider:

| Factor | Points Toward |
|--------|---------------|
| Greenfield project | Option A (structure from start) |
| Well-structured existing code | Option A (already there) |
| Restructuring feasible | Option A (invest in structure) |
| Restructuring not feasible | Option B or C |
| Large legacy codebase | Option C (freeze baseline) |
| Small project, simple architecture | Option D (code quality only) |
| Architecture not yet defined | Option D (premature to enforce) |
| Need guardrails immediately | Option B, C, or D |

**Hybrid approaches are valid.** You might:
- Start with Option D (code quality) while planning restructuring
- Use Option C (freeze baseline) during a migration to Option A
- Apply Option A to new code while using Option C for legacy areas

## Step 5: Guardrail Categories to Implement

Once you've chosen an approach, implement guardrails in these categories:

### Code Quality Guardrails

Universal checks that apply regardless of architecture:

- **Type checking** - Static type analysis appropriate to the language (TypeScript, mypy/pyright, etc.)
- **Complexity limits** - Cyclomatic complexity, function length, nesting depth
- **Linting** - Language-appropriate linter with project-specific rules
- **Formatting** - Automated, non-negotiable formatting (Prettier, Black, gofmt, etc.)

### Architectural Guardrails (If Applicable)

If you chose Option A, B, or C:

- **Import/dependency rules** - What can depend on what
- **Forbidden patterns** - Anti-patterns detected via static analysis
- **Layer boundaries** - Higher layers depend on lower, not vice versa
- **Forbidden calls** - Methods/functions that shouldn't be called from certain contexts

### Agent Process Guardrails

Constrain what AI agents can do:

- **Block bypass attempts** - Prevent `--no-verify`, hook disabling, config overrides
- **Block dangerous operations** - Prevent merging PRs, force-pushing, destructive commands
- **Enforce completion protocols** - Require structured completion (not just stopping mid-task)
- **Session boundary enforcement** - Warn or block if agent exits without proper completion

### Git Hook Guardrails

Enforce validation at key moments:

- **Pre-commit** - Fast checks (formatting, types, obvious errors)
- **Pre-push** - Full validation (tests, linters, architecture checks)

**Principle:** Pre-push is the canonical local gate. If it passes locally, CI should pass.

### Test Guardrails

Ensure tests are meaningful and isolated:

- **Environment isolation** - Tests don't pollute each other or the real environment
- **Infrastructure markers** - Mark tests that need external services, so they can be skipped or run separately
- **Adversarial tests** - Tests that attempt to bypass guardrails (prove the guardrails work)

### Validation Orchestration

Create validation commands that:

- Runs all guardrails
- Uses parallelism for speed
- Provide fast feedback for quick agent/reviewer loops
- Match what pre-push runs for publish readiness
- Mirror CI coverage where practical

**Example pattern:**
```
make validate-quick   # Types + unit tests (~30s)
make validate         # Full local validation (~2min)
make validate-full    # Including slow/integration tests (~5min)
```

## Step 6: Create Implementation Plan

Now that you've chosen an approach and identified which guardrail categories apply, create a detailed implementation plan.

For each guardrail category:
1. Identify the specific tools for your language/ecosystem
2. Define the rules/configuration
3. Decide where it runs (pre-commit, pre-push, CI)
4. Plan the rollout (all at once, or incremental with baseline freezing)

**I can help create a detailed implementation prompt tailored to:**
- Your chosen approach (A, B, C, or D)
- Your specific language/ecosystem
- Your project's current state
- Available tooling for your stack

Just describe your situation and I'll create a specific implementation guide.

---

## Reference Implementation

For a complete example of these patterns in a Python codebase, see the issue-orchestrator project:

| Component | Location | What It Does |
|-----------|----------|--------------|
| Hook enforcement docs | [docs/architecture/hooks.md](../architecture/hooks.md) | Multi-layer hook architecture |
| AST guardrails config | [tools/ast_guardrails.yml](../../tools/ast_guardrails.yml) | Forbidden patterns and calls |
| AST guardrails script | [tools/check_arch_guardrails.py](../../tools/check_arch_guardrails.py) | Custom static analysis |
| Import-linter config | [pyproject.toml](../../pyproject.toml) | Layer boundary contracts |
| Claude Code hooks | [.claude/settings.json](../../.claude/settings.json) | Agent process constraints |
| Block bypass script | [.claude/hooks/block-no-verify.sh](../../.claude/hooks/block-no-verify.sh) | Prevents --no-verify |
| Makefile validation | [Makefile](../../Makefile) | Validation orchestration |

These demonstrate Option A (structure reflects architecture) with comprehensive guardrails across all categories.
