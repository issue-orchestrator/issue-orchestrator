# Quality Guardrails

This repository uses ratcheted quality guardrails for architecture and control-plane drift.

The goal is not to pretend the current codebase has no debt. The goal is to make new debt visible, fail PRs that make tracked metrics worse, and then reduce the baseline through focused cleanup PRs.

This grew out of the control-architecture discussion around issue #6362 and the follow-up decision to stop relying on longer agent prompts for quality. The intended model is mechanical: whole-repo ratchets for systemic drift, changed-code checks for local regressions, and later Semgrep/CodeQL-style rules once the repo-local metrics have proven useful.

## Current Guardrails

Run:

```bash
make quality-guardrails
```

The command runs `tools/quality_guardrails.py` against `tools/quality_guardrails.yml` and compares the results with `quality/guardrails-baseline.json`.

The first rule set tracks:

- oversized control hotspots
- branch sites that mention lifecycle/control vocabulary

These are proxies for the failure pattern captured in issue #6362: control policy spreading across multiple owners, projections, and execution paths.

## Ratchet Model

Existing violations are stored in the baseline. A PR fails when it:

- introduces a new tracked metric that is already over the configured threshold
- increases a tracked metric above its baseline value

Rules may define `new_metric_min_value` so small new files can be reported without failing the ratchet immediately. For example, the lifecycle/control branch-site rule starts failing unbaselined files at three matching branch sites.

Improvements do not fail. If a cleanup PR removes policy sites or shrinks a hotspot, regenerate the baseline and commit the lower value.

```bash
python tools/quality_guardrails.py --update-baseline
```

For ordinary PRs that intentionally add exactly one tracked metric, prefer targeted acceptance instead of regenerating the whole baseline:

```bash
python tools/quality_guardrails.py --accept control_policy_branch_sites:src/issue_orchestrator/control/new_owner.py
```

Targeted acceptance updates only the named key, then re-runs the ratchet comparison. Any unrelated increases remain violations.

## Adding Guardrails

Add guardrails in small PRs:

1. Add the checker in report/ratchet form.
2. Baseline the current repository state.
3. Fail only new or worsened findings.
4. Create separate cleanup PRs to reduce the baseline.
5. Promote mature checks to hard gates when the baseline reaches zero or a defensible threshold.

Good guardrail candidates:

- semantic status/reason vocabulary duplication
- owner-boundary bypasses for labels, sessions, artifacts, and cache state
- raw untyped command/event/artifact payloads at public seams
- dead legacy UI/control surfaces
- dependency topology and change-coupling hot spots

Prefer mechanical checks over prompt instructions. If a rule is important enough to rely on, encode it in tooling.
