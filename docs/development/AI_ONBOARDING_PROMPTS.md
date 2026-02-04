# AI Onboarding Prompts

## Setup-First Onboarding -> Issue Creation

Assumption:
- A setup utility exists and is the intended starting point.
- The user runs the setup utility before reading documentation.

Rules:
- Start with the setup utility. Do not read README/docs unless blocked.
- Record every obstacle with exact command, error, and suggested fix.
- Classify each issue: setup friction, missing prerequisite, UX confusion, bug.
- Create GitHub issues instead of a report.
- Use repo: `BruceBGordon/issue-orchestrator`.
- Use labels: `ai-discovered-setup-problems`, `agent:backend`.
- Use milestone: `Milestone 1`. Create it if missing.
- Do not create duplicate issues. Search existing issues by title keywords and label.
- If a duplicate exists, add a comment with new evidence and stop.
- Group issues when they share a root cause or a single user step.

Grouping strategy:
- Bundle by root cause when multiple small problems share the same failure.
- Bundle by user step when multiple small problems occur in one step.
- Keep separate when fixes require different areas, have different severity, or are independently testable.
- If there are 3 or more tiny doc or UX clarity problems, group them into one issue with a checklist.
- If the setup utility hard-fails, always create a dedicated issue.

Task:
1. Run the setup utility. Choose the default or recommended options.
2. Immediately attempt the simplest “hello world” run.
3. Start the orchestrator in the simplest UI mode.
4. Create a test issue in the configured repo.
5. Confirm the issue is picked up and a session starts.
6. Attempt completion using the documented flow (agent-done).
7. Verify outcome: labels changed as expected, PR created or dry-run behavior clearly indicated.
8. If anything fails, consult README/docs only then, and note whether it helped.
9. Create issues for all friction points, applying the grouping strategy.

Issue template:
- Title: `Onboarding: <root cause or step>`
- Body sections: What happened, Expected, Steps to reproduce, Why this matters, Proposed fixes.
- Proposed fixes should be a checklist.

