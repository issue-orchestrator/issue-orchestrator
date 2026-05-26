# Quality Guardrails

This repository uses ratcheted quality guardrails for architecture and control-plane drift.

The goal is not to pretend the current codebase has no debt. The goal is to make new debt visible, fail PRs that make tracked metrics worse, and then reduce the baseline through focused cleanup PRs.

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

Improvements do not fail. If a cleanup PR removes policy sites or shrinks a hotspot, regenerate the baseline and commit the lower value.

```bash
python tools/quality_guardrails.py --update-baseline
```

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
