---
name: readiness
description: Assess whether a target repo has the prerequisites for AI-agent orchestration to be effective — PR/CI discipline, reviewer in place, issue sizing, architecture documented and enforced, tests at public boundaries, mechanical definition-of-done, abstraction quality. Conversational chaperone — escalates checks (installs, probes) only with per-step authorization. Use before scaling agent work on a repo, when an orchestrator is burning cycles and you suspect repo discipline, or to periodically re-score. Supports a **read-only mode** the user requests in their prompt (e.g., "in read-only mode"); in that mode the skill restricts itself to static inspection and read-only API calls — no installs, probes, or remote writes. This is a request-mode, not a CLI flag.
---

# Readiness

Assess whether a repo has the prerequisites for AI-agent orchestration to be effective.

This skill is **conversational, not a one-shot scanner**. Walk the user through the rubric, escalating from cheap static checks to installs/probes only with explicit per-step authorization.

## Anchor

The rubric is a flat list of pillars. Each pillar describes an **outcome** an agent needs, names a cheap **proxy** check, the **gap** the proxy can't see, and a classification (**mechanical** = installable/configurable, **structural** = needs design work, **process** = team practice). Some pillars also have an opt-in **probe** for users who want airtight verification.

Pillars are **independent failure modes**. Do not collapse them under a unifying theme. Abstraction quality is one pillar among the others, not a master frame.

## Step 0 — Mode question

Ask first; the rubric forks based on the answer:

- **self-framework** — this repo *is* the disciplined system. Full rubric applies.
- **client of external framework** — repo configures/extends an upstream platform; architecture lives elsewhere. Pillars 3–5 and 9 shift to integration concerns (see per-pillar notes).
- **hybrid** — both. Apply both readings, weighted.

If the user is unsure, propose detection (size, dependency footprint, presence of obvious extension-point patterns) and confirm. Do not auto-decide.

## Operating rules

- **No installs, network writes, or probe pushes without per-step authorization.** Do not bundle authorizations across pillars.
- **Read-only mode** (request-only, not a CLI flag — user says e.g. "in read-only mode" or "without installs/probes" in their prompt): zero installs, zero probes, zero remote writes. Pillars unverifiable without those tools are reported as `unverified`, not `failing`. The report explicitly notes its bounded scope.
- **Late-binding detection**: do not assume specific tools. Examine the repo, identify language/build system, reason about appropriate checks. Web search for current tooling is allowed with user OK. The skill does not ship with detector packs — detection is a research problem solved at runtime.
- **Cite evidence inline** — file paths, command outputs, line numbers. Don't assert without showing.
- **Acknowledge proxy gaps explicitly** in the report.
- **If a pillar is unverifiable** in this repo (e.g., no CI runs to inspect), say so rather than failing it.

### Rigor rules (added v0.1 after error in first runs)

These rules exist because the agent previously claimed structural pillars `passing` from aggregate metrics that washed out per-instance variance.

- **Right unit for the claim.** Each pillar's proxy specifies the **unit of measurement** (file, class, module, PR, issue). Measure at that unit. **Do not aggregate when the claim is per-instance** — e.g., "modules are deep" is a per-module claim; aggregating across a package can hide both wide-shallow modules and god classes.
- **Distribution before aggregate.** For any measurement, show the *distribution* (top-N largest, top-N most public, bottom-N) before any summary statistic. Variance is usually the signal.
- **Sampling-and-reading is mandatory for structural `passing`.** Pillars 5, 9, and 10 cannot be reported as `passing` from measurement alone. Sample N representative items (default N=3–5), open them, read them, and reason. Measurement alone caps the verdict at `gappy` with an explicit note about the missing sample.
- **Pre-verdict falsification check.** Before claiming `passing` on any pillar, write down the failure mode the verdict assumes is absent, and confirm it. Example for Pillar 10: "deepest files might be god classes" → must read them to rule that out before claiming pass.
- **Cross-pillar contamination is not allowed.** Evidence for one pillar does not count as evidence for another. Specifically: evidence for Pillar 3 (architecture documented), Pillar 4 (enforced), or Pillar 9 (coherence) does NOT count as evidence for Pillar 10 (depth). Pillar 10 requires per-file/per-class evidence; the other pillars don't.

## Pillars

### 1. PR-only merge path (mechanical)
- **Outcome**: changes to the default branch arrive only via reviewed PRs.
- **Proxy**: branch protection API shows protection enabled, direct push blocked, PR required.
- **Gap**: doesn't catch admin-bypass culture.
- **Probe**: try a direct push to a throwaway branch; observe rejection.
- **Client-mode**: same.

### 2. Mechanical merge gate (mechanical)
- **Outcome**: merging is gated on automated checks that catch correctness regressions.
- **Proxy**: required status checks on default branch include at least typecheck (where applicable), lint, tests; checks run on `pull_request`.
- **Gap**: doesn't catch "checks pass but cover nothing" — fast-but-empty test suites.
- **Probe**: introduce a deliberate type/lint/test failure on a feature branch; observe CI behavior.
- **Client-mode**: same.

### 3. Architecture documented (structural)
- **Outcome**: a reader can answer "where does feature X go?" without grepping the whole repo.
- **Proxy**: presence and currency of `CLAUDE.md` / `ARCHITECTURE.md` / ADRs / module READMEs that name responsibilities, with references that resolve to the current code.
- **Gap**: doc may exist but be stale or contradict the code; static check can't tell.
- **Probe**: ask the agent to locate where a hypothetical typical issue would land using only the docs; observe whether it succeeds without grepping.
- **Client-mode**: shifts to **integration architecture documented** — where vendor calls live, what the adapter layer looks like, how upgrades are handled.

### 4. Architecture enforced (mechanical or structural)
- **Outcome**: architecture violations are caught before merge.
- **Proxy**: language- and tool-appropriate enforcement is wired into a required check. In strongly-typed languages, **compiler-enforced module privacy counts** (Go `internal/`, Rust `pub(crate)`, TS `"exports"` map, Java package-private + ArchUnit, etc.) — credit it without demanding additional tooling. In weakly-typed languages, an explicit linter is needed. Reason about what's appropriate for this repo's stack.
- **Gap**: tool can be installed with no rules configured. Proxy passes, reality fails.
- **Probe**: introduce a planted boundary violation; observe whether build/CI rejects it.
- **Client-mode**: shifts to **vendor surface isolated** — vendor SDK imported in one thin adapter, not scattered across modules. Version-pinned with an upgrade story.

### 5. Tests survive internal refactors (structural)
- **Outcome**: renaming or moving an internal symbol doesn't cascade-break tests; failures point to behavior changes, not structural ones.
- **Unit of measurement**: the **test file**, with classification of its mock/patch targets (not aggregate counts across the suite).
- **Proxy**: sample 5–10 test files randomly; for each, classify every `mock.patch` / `mock(...)` / sinon stub / Mockito mock as one of: **port-level / DI-boundary** (legitimate), **external-system stand-in** (legitimate), **internal-symbol** (fragile). Aggregate ratios across the suite are explicitly NOT sufficient — they cannot distinguish legitimate from fragile mocking.
- **Gap**: even per-file classification can be wrong about whether a "port" is actually narrow. The probe is what gives certainty.
- **Probe**: rename or move an internal symbol on a feature branch; run tests; count incidental failures (test changes unrelated to behavior).
- **Verdict ceiling (read-only)**: aggregate mock count alone → at most `gappy`. Per-file classification of a sample → can reach `passing`. Probe → `passing` confirmed.
- **Client-mode**: shifts to **contract tests against vendor surface** — tests pin the integration's expectations of the upstream API.

### 6. Definition of done is mechanical (mechanical)
- **Outcome**: when an agent declares "done," an automated signal confirms or denies — humans verify intent, not basics.
- **Proxy**: explicit DoD documented (CONTRIBUTING.md, orchestrator config, agent prompts); the DoD's checks run on PRs; orchestrator's `coding-done`/`reviewer-done` is integrated where applicable.
- **Gap**: a written DoD that nobody enforces. Distinct from Pillar 2 — DoD is broader (includes pre-PR self-checks, agent-side validation), not just CI gates.
- **Probe**: review last N PRs; count human comments that flagged things automation should have caught.
- **Client-mode**: same.

### 7. Reviewer in place (process)
- **Outcome**: every PR receives substantive review; reviewers find things the agent missed.
- **Proxy**: CODEOWNERS exists; branch protection requires reviews; a review agent is configured if applicable.
- **Gap**: required reviews can be rubber-stamped — protection-required ≠ review-actually-happens.
- **Probe**: sample recent PRs; measure review-comment density and whether reviewers have ever requested changes.
- **Client-mode**: same.

### 8. Issues are right-sized (process)
- **Outcome**: typical issues complete in one agent session without scope creep or escalation.
- **Proxy**: sample 20 recent closed issues — presence of acceptance criteria, median time-to-close, scope-change frequency, escalation rate.
- **Gap**: well-formatted issues can still be too big; format is not size.
- **Probe**: pick a typical-looking open issue; estimate (or run) whether one session would resolve it.
- **Client-mode**: same.

### 9. Abstraction quality (structural — judgment-heavy)
- **Outcome**: concepts are named; the codebase reflects coherent design rather than accretion; the language used in code matches the language used to talk about the domain.
- **Proxy**: structural signals — cohesion via co-change (`git log --name-only`), fan-in/fan-out, primitive obsession in signatures, naming consistency between code and domain docs/issues. **Show samples and reason; do not assert from a metric alone.**
- **Gap**: the hardest pillar to evaluate fairly. Static analysis cannot tell you whether the chosen abstraction names the *right* thing or captures the domain.
- **Probe**: agent samples 3–5 modules, summarizes each module's responsibility in one sentence; user assesses whether the summaries match their mental model. Misalignment is signal. Optionally: extract domain vocabulary from issues/READMEs and compare against code symbols; named drift is signal.
- **Client-mode**: adds **vendor surface itself is reasonable** — if the upstream API is undocumented chaos, no client-side discipline saves you.
- **Note**: findings here are usually **structural** (not 10-minute fixes). If abstraction quality is poor enough, the orchestrator may not be effective even with all mechanical pillars green. Say so plainly in the summary.
- **Distinction from Pillar 10**: Pillar 9 is about *coherence and naming* (does the codebase make sense conceptually?). Pillar 10 is specifically about *API surface vs. internal substance* (do modules hide complexity behind narrow APIs?). They fail in different ways and have different fixes.

### 10. Modules are deep (structural)
- **Outcome**: each module hides substantial internal complexity behind a narrow public API. Most refactoring changes a module's internals without changing its public face. Refactoring blast radius is small.
- **Unit of measurement (per language)** — **NOT a directory or top-level package**. The unit is:
  - **Python**: a **file** (or a class within a file). Aggregate per-package metrics WILL hide variance; do not use them as evidence.
  - **Kotlin/Java/C#**: a **class**. File-level is a rough proxy when one file = one top-level class.
  - **TypeScript**: a **module file** (`.ts` / `.tsx`).
  - **Go**: a **package** (a Go package is one cohesive unit; this is the exception).
  - **Rust**: a **module** (`mod` block or file).
- **Proxy** — internal-LOC vs. public-symbol ratio at the unit above:
  - **Python**: count `^(class|def|async def) [a-zA-Z]` per file vs. file LOC
  - **Kotlin**: count default-public top-level declarations (`^(class|object|interface|fun|data class|sealed class|sealed interface|enum class) `) per file vs. file LOC; **bonus signal** — count of `^internal ` declarations indicates deliberate hiding
  - **TypeScript**: count `^export ` per file vs. file LOC
  - **Go**: count capital-letter top-level identifiers per package vs. package LOC; presence of `internal/` directories is a strong positive signal
  - **Rust**: count `^pub ` per module vs. module LOC; `pub(crate)` granularity is the depth control
- **Required output before any verdict** — show the **distribution**, not just an aggregate:
  - Top-15 files by LOC (potential deep modules or god classes)
  - Top-15 files by public-symbol count (potential wide-shallow grab-bags)
  - Histogram of public-symbol count (how many files have 1, 2, 3, … public symbols?)
- **Interpretation** — read by layer, not single threshold: definitional layers (contracts, types, ports, domain models, schemas) are *legitimately shallow* by design — they exist to expose names. Behavioral layers (adapters, infrastructure, controllers, components, services) should be deep. Compare like-to-like, not across layer kinds.
- **God-class boundary**: a single file at >1,500 LOC with 1–2 public symbols is structurally "deep" but is at the size boundary where "one cohesive responsibility" becomes implausible. Mandatory sample-and-read for any file in this zone before claiming `passing`.
- **Cross-language note**: in strongly-typed languages with module visibility (Kotlin `internal`, Rust `pub(crate)`, Go `internal/`, Java package-private), a count of zero "internal hiding markers" in implementation modules is a smell — the language gives you depth-enforcement for free and the codebase isn't using it.
- **Gap**: the metric tells you about *quantity* of public surface, not *quality*. A module with 50 well-named focused methods scores the same as 50 random utilities. The depth score also doesn't tell you whether deep modules are *coherently* deep (one concept) or *bloated* (many concepts crammed together). Pillar 9's coherence check complements this.
- **Probe (required for `passing`)**: sample the **deepest 2–3 files** and any **wide-shallow grab-bag candidates** (high LOC AND high public-symbol count). Read them. For deep ones: does the public API have a clear single responsibility, or is it a grab-bag of unrelated entry points? For shallow ones: are they meant to be definitional (good) or are they wide-shallow leaks of internal structure (bad)? Reading is cheap and dispositive.
- **Verdict ceiling**: distribution measurement alone → at most `gappy`. Distribution + sampling-and-reading → `passing` or revised verdict based on what reading reveals.
- **Pre-verdict falsification check** for `passing`: "the deepest files are NOT god classes" — confirm by reading.
- **Distinct from Pillar 9**: evidence for Pillar 9 (layered architecture, naming, conceptual integrity, hexagonal layout, DDD docs) does NOT count for Pillar 10. They are orthogonal — a perfectly layered codebase can have all wide-shallow modules; a poorly layered one can have all deep modules.
- **Client-mode**: shifts to **the integration adapter is deep** — a thin client of an external SDK should have one deep adapter (narrow public API hiding vendor calls) and a few thin extension points elsewhere. A wide-shallow adapter that just re-exposes the vendor surface is the failure mode.

### 11. Tests grow with code (process)
- **Outcome**: new code ships with tests; coverage trend is stable or growing. A repo can pass every other pillar today and still bleed coverage if PRs don't add tests as they add code.
- **Unit of measurement**: the **PR** (a sample of recent merged PRs), classified by what file types it touches.
- **Tooling-aware confidence gradient** — running the coverage tool itself requires installation + test execution, which read-only mode cannot safely do. So evidence comes in tiers, each with its own verdict ceiling:

  | Tier | Evidence available | Max verdict |
  |---|---|---|
  | **A** | Coverage tool configured **and** threshold enforced in CI **and** recent CI runs show actual numbers **and** PR classification healthy **and** source-only samples read | `passing` |
  | **B** | Coverage tool configured, threshold visible in config, PR classification healthy (no observed coverage number) | `gappy` |
  | **C** | No coverage tool detected, but PR classification shows discipline | `gappy` with explicit confidence-reduction note |
  | **D** | No coverage tool, source-only rate is high | `failing` |

- **Proxy** — three layered signals:
  1. **Coverage tool with enforced threshold** in CI (e.g., `jacocoTestCoverageVerification`, `--cov-fail-under`, `nyc check-coverage`). Presence is positive but the *actual coverage number* requires running the tool — that's a probe.
  2. **PR classification** — sample 15–20 recent merged PRs (skip pure-docs and pure-deps); classify each as `adds-tests` (source + tests), `tests-only` (fine), `source-only` (no tests — the smoke), `docs-only`, `config-only`.
  3. **Source-only rate** — fraction of source-touching PRs that ship without tests. Low (≤10%) healthy; high (≥30%) is the "losing battle" failure mode.

- **Tooling-absent is itself a finding**, not a neutral gap. If no coverage tool is configured anywhere in the repo, that materially reduces confidence in the verdict no matter how clean the PR classification looks — because we can't catch the failure mode "every PR adds *some* tests but coverage still drops because new tests don't cover the new code." The report must say this out loud, not paper over it.

- **Probes (read-only blocks both)**:
  - **Probe A**: install (or invoke an already-installed) coverage tool; run the test suite with coverage; report actual % and per-module breakdown. Intrusive — needs install permission and test execution.
  - **Probe B**: fetch the most recent CI run's coverage artifact (if the workflow publishes one). Less intrusive than running locally; depends on the workflow exposing the report.
  - **Probe C**: read 2–3 source-only PRs and assess whether they *should* have included tests. Read-only; cheap.

- **Distinct from Pillar 5**: Pillar 5 is about test *structure* (where tests target). Pillar 11 is about test *economics* (do we keep adding them?). Orthogonal failure modes.
- **Distinct from Pillar 6**: Pillar 6 asks whether the DoD is mechanical. Pillar 11 asks whether the DoD's test requirement is *actually being met by recent work* (the outcome, not the policy).
- **Client-mode**: same. The vendor integration also needs tests; contract tests against vendor expectations is the form (per Pillar 5 client-mode).

## Conversational flow

1. **Mode question** (Step 0).
2. **Inventory** — read top-level structure, build files, language(s), CI config, branch protection (read-only API call). Show what was inventoried.
3. **Cheap pass** — run zero-cost checks for all applicable pillars. Cite evidence. Mark each `passing` / `gappy` / `failing` / `unverified`.
4. **Report draft 1** — show the user the cheap-pass results before doing anything more.
5. **Escalation discussion** — for each `gappy` or `unverified` pillar, propose specific escalations (install X, probe Y, web-search current tooling) with rough cost/intrusiveness. User says yes/no per item. Do not bundle.
6. **Run authorized escalations.** Update findings.
7. **Final report + punch list** — generate draft GitHub issues for failing/gappy pillars, classified mechanical vs. structural vs. process.
8. **Honest summary** — one paragraph: what kind of agent work this repo is ready for, what it isn't, the highest-impact next step.

## Output

Markdown report at a path the user picks (default: `./readiness-report.md`):

- **Header**: date, repo, mode, run scope (full / read-only / partial-by-user-opt-out).
- **Per pillar**: outcome statement, evidence cited (with file paths / command output), verdict, what was checked, what was skipped and why, classification.
- **Punch list**: draft GitHub issues; mechanical first (cheaper wins), then structural and process; each with a one-paragraph problem statement and a suggested approach. Issues are draft text — the user creates them on GitHub manually unless they explicitly authorize otherwise.
- **Honest summary**: 3–5 sentences. **Not a numeric score** — a qualitative readiness statement.

If read-only mode was requested: the report's header and summary explicitly state "no installs, no probes, no remote writes — this assessment is bounded by what static inspection can see."

## Anti-patterns

- **Don't** collapse pillars under "abstraction-first" or any other unifying theme. Pillars are independent failure modes.
- **Don't** assert abstraction quality from a metric. Show samples; reason about them; let the user weigh in.
- **Don't** aggregate when the claim is per-instance. "Modules are deep" is a per-module claim — a top-level-package ratio can wash out both wide-shallow modules and god classes. Aggregates are summaries, not evidence.
- **Don't** treat tool-presence as tool-running. Coverage tool configured ≠ coverage actually measured; import-linter installed ≠ contracts actually enforced; CI workflow defined ≠ CI required to pass. Configuration is one signal; behavior is another. Say which you have.
- **Don't** let evidence for one pillar leak into another. Layered architecture (Pillar 3/4) is not evidence that modules are deep (Pillar 10). DDD docs are not evidence that abstractions are coherent in code. Each pillar's verdict needs its own per-pillar evidence at its own unit.
- **Don't** skip sampling for structural pillars. Pillars 5, 9, 10, 11 require reading representative items. Measurement alone caps the verdict at `gappy`.
- **Don't** ship pre-built detector packs or assume Python-flavored tooling. Reason about detection per repo at runtime, accounting for the language family.
- **Don't** auto-install or auto-probe. Always ask, per item.
- **Don't** claim a pillar is failing when you only have a gap. Mark it `unverified` and name the missing evidence.
- **Don't** present a numeric score. The report is qualitative; a score implies commensurability across pillars that doesn't exist.
- **Don't** skip the mode question. The rubric forks; running the wrong fork produces meaningless punch-list items.
- **Don't** create GitHub issues, push branches, or modify remote state without explicit per-action confirmation.

## When to stop and ask

Stop and ask the user when:
- The mode is genuinely ambiguous (mixed indicators of self-framework vs. client).
- A probe would write to remote state (push, install, network call beyond reads).
- A pillar's evidence is contradictory (e.g., branch protection enabled but recent direct-push commits visible).
- Abstraction-quality samples disagree with each other and the user's confirmation is needed to pick a direction.

## Iteration intent

This is v0. Expect to refine pillars, proxies, and probes based on real onboardings. Track surprises — places where a proxy lied, places where a pillar didn't matter, places where a missing pillar caused trouble — and feed them back into the skill. The detector knowledge that hardens over time may eventually warrant codification, but only after observed evidence justifies it.
