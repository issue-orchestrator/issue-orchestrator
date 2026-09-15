"""How a tech-lead agent must READ the board snapshot (#7080, #6969).

Two sections of the tech-lead prompt, kept here rather than inline in
:mod:`.setup_wizard_prompts`, which already carries five prompts and had grown
past its size budget. They belong together: both are rules about not drawing a
false conclusion from the snapshot -- one about the tech lead misreading its own
health, one about it misreading whose work a timeline record was.
"""

#: Whether the tech lead's OWN decisions are reaching GitHub (#7080).
TECH_LEAD_WRITE_HEALTH_SECTION = """**`tech_lead_write_health` is about you, and it outranks everything else on the
board.** It compares how recently a tech-lead run was requested against how
recently a tech-lead decision actually reached GitHub. When `is_alarm` is true,
report it as a finding before anything else you found, and follow the verdict:

- `proposing_only` - runs ARE deciding and nothing is being applied. Do NOT
  re-diagnose the runs. Look at the approval gate (`tech_lead.authority.*`
  entries set to `propose` create gated proposal issues that stay inert until an
  operator removes the `proposed-tech-lead` label) and at the act-level
  appliers. Count the outstanding gated proposals and name them.
- `silent` - runs continue and are producing neither proposals nor applied
  decisions. Something upstream of the decision is failing; the runs themselves
  are where to look.
- `idle` - no runs requested inside the window. Nothing to report.
- `writing` - healthy.

This signal exists because the subsystem once ran ten days requesting runs and
applying not one decision, consuming agent capacity and manufacturing
`blocked-failed` labels the whole time, and every health review in that window
read a board snapshot that said nothing about it (#7080). A silent tech lead is
worse than a disabled one; do not let this one pass unreported."""

#: Which session produced each record in a per-issue extract (#6969).
TECH_LEAD_TIMELINE_ACTOR_SECTION = """**Reading a timeline extract: check `timeline_actor` on EVERY record.** An
issue's timeline is keyed by its issue number, but not every record in it is
that issue's own work. A tech-lead FAILURE INVESTIGATION runs under its focus
issue's number in a disposable scratch worktree, so its own review exchange,
branch push and completion land on the focus issue's timeline. Each record
therefore carries `timeline_actor`, and each extract carries `actor_counts`:

- `issue-session` - the issue's own coding/rework/review work. Only these are
  evidence about the issue's implementation.
- `tech-lead-investigation` - a tech-lead session that merely READ this issue.
  Its `review.approved` approved the INVESTIGATION branch and its "Pushed branch
  to remote" pushed the INVESTIGATION branch. Never report it as the issue being
  approved, reviewed, or published.
- `unknown` - written before the discriminator existed and not attributable from
  durable identity. Treat it as NO evidence either way; corroborate against the
  run_dir and the PR before concluding anything.

This is not hypothetical: the 2026-08-03 health review read issue #6410's extract,
saw an approval and a push, and reported the implementation as "review-approved
(2 rounds)". It was not - all five exchanges ended in error with
changes_requested - and that false conclusion was copied into recovery guidance
that would have merged never-approved branches (#6969)."""
