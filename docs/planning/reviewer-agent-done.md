# Reviewer completion command (reviewer-done)

## Motivation
During via-local-loop runs the reviewer currently calls the exact same `coding-done` CLI as the coder. That means every review round reruns `make validate-quick` and the `push_preflight` guard even though reviewers are not making new commits. The extra validation is redundant and, as we just observed, exposes a timing window where the reviewer process executes before `HEAD` is reattached, leading to a false "Could not determine current branch" failure.

We need a lighter reviewer completion signal so that reviewers can simply approve or explain issues while the orchestrator still keeps trust in the flow.

## Proposed workstream
1. Add a reviewer-specific completion command `reviewer-done` (separate from `coding-done`).
2. When that flag is set:
   - Skip rerunning validation if there is a cached record for the commit the reviewer just inspected (we already capture the coder’s validation result in the reviewer run directory).
   - Skip `push_preflight` unless the reviewer actually modified files (we can detect dirty files before running the guard). If no changes exist, reuse the metadata for branch/commit to avoid the detached-HEAD signal.
   - Emit diagnostics so we can prove the reviewer saw the same validation as the coder.
3. Keep the existing coder path unchanged so `coding-done` still runs full validation/push guards for any writer.

## Ramifications
- The completion system becomes two commands (`coding-done` and `reviewer-done`); the orchestrator must understand both signals and only push/create the PR once per issue.
- Validation caching must be explicit and serialized; if the reviewer finds a regression, the command needs a clear way to request a fresh run (`coding-done completed --reuse-validation=never`).
- Documentation and the README/AGENT_PROTOCOL must call out `reviewer-done` so reviewers know when to reuse validation and when to rerun it.
- Tests covering validation/push guards will need to cover both commands (existing behavior stays the same for `coding-done`, new tests cover `reviewer-done`).

## Next steps
- Discuss the desired UX (flag name, behavior when the reviewer needs to rerun validation) with the team.
- Update the `AGENT_PROTOCOL.md` and docs to describe the new reviewer signal.
- Implement the CLI changes and guardrail adjustments once the feature is green-lit.
