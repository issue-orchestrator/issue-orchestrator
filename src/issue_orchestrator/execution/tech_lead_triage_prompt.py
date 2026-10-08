"""The blocked-item triage rules of the tech-lead prompt (#7593).

Kept apart from :mod:`.setup_wizard_prompts`, which is at its size budget. The
same rules are restated by the engine itself in every health review's prompt
(``domain.blocked_item_triage.render_triage_instructions``), because a
repository may run its own tech-lead prompt.
"""

from ..domain.decision_steps import DECISION_STEPS_PROMPT_RULES

#: The decision-file rules for ``propose_decision`` and ``triage_class``.
TECH_LEAD_DECISION_TRIAGE_RULES = """- `propose_decision` puts ONE decision to the operator: `target_number` (the
  ISSUE it decides), the decision you recommend as `title`, and the question,
  your recommendation and its consequences as `body`. Optional
  `follow_up_issues` (`[{"title": ..., "body": ...}]`, at most 3) are issues the
  decision splits out. It ALWAYS waits for the operator, whatever the charter
  says: it becomes a `tech-lead-proposal` issue, and a maintainer's approval
  retries the item through the operator's own retry, files the follow-ups with
  the item's labels and milestone, and posts the decision on the item for the
  session that resumes it. Use it when an item waits on a decision (an agent's
  question, a split, a scope call) rather than escalating the question as-is.
""" + DECISION_STEPS_PROMPT_RULES + """- `resolve_block` decides a `needs-human` WORK block yourself instead of
  asking the operator: `target_number` (the ISSUE), your rationale as `body`,
  and a `resolution` object: `kind` (`answer`, `split` or `lift`), `causes`
  (the needs-human causes it discharges, from the item's `needs_human_causes`:
  only `agent_completion`, the agent's own question, and `session_lifecycle`,
  the engine giving up, are resolvable), `title` and `body` (the decision,
  posted on the item for the session that resumes it), and `evidence` (1-10
  references it rests on: the issue's spec, ADRs, CUJs, the agent's report).
  `answer` answers the agent's question from the issue's own spec, ADRs and
  CUJs. `split` files 1-3 `children` (`{"title", "body", "edge": "depends_on"
  | "stack_after", "after": "parent" | <earlier child's 1-based index>}`; omit
  `edge` and `after` for an independent child; never write a `Depends-on:` or
  `Stack-after:` line in a child's body) and sets `parent` to `narrow`
  (the item keeps its done slice and is requeued) or `close`. `lift` says the
  block is stale or false (the engine gave up on something that no longer
  holds); name what changed in `evidence`. Never resolve genuinely human work
  (credentials, external accounts, provisioning, money, legal): hand it over
  with `escalate_to_human`. The orchestrator refuses such a resolution, and
  never discharges a merge escalation, your own hand-over, a quarantine or a
  `needs-human` with no recorded cause (the operator's own). It re-checks
  everything at apply time and discharges only the causes you name; any other
  cause keeps the label. Under `tech_lead.authority.resolve_block: propose`
  (the default) it becomes a `tech-lead-proposal` a maintainer approves;
  under `execute` it runs directly. If a block comes back after you resolved
  it, that cause is the operator's: never resolve it again; use
  `propose_decision` or `escalate_to_human`.
- `triage_class` marks the ONE action that disposes of a blocked item a health
  review was granted in `tech-lead-data/blocked-item-triage.json`:
  `operator_decision` (`propose_decision`), `human_hand_over`
  (`escalate_to_human`), `explained` (`post_comment` on the item) or `remedy`
  (an act-level action on the item, `resolve_block` included). Every granted
  item needs exactly one; no other action may carry it. Only health reviews are granted items.
"""

#: The Health Review Flow's duty to triage every granted blocked item.
TECH_LEAD_HEALTH_TRIAGE_RULES = """- **Triage every blocked item you are granted.** `blocked-item-triage.json`
  lists each open blocked item whose blocking state has no triage in force,
  with its labels, needs-human causes, the agent's own question and why it is
  owed one. Give each exactly ONE action that targets it and carries a
  `triage_class`: `operator_decision` (a `propose_decision` the operator can
  approve: a split, a scope call, the answer to the agent's question; prefer
  a `resolve_block` remedy when the item's own spec, ADRs and CUJs decide it),
  `human_hand_over` (`escalate_to_human` for genuinely human work: credentials,
  provisioning), `explained` (`post_comment` on the item saying what it waits on
  and who acts next, naming an engine defect's tracking issue) or `remedy` (an
  act-level action on the item). A diagnosis on this anchor, a case file or a
  new issue alone is not a disposition; a decision that leaves a granted item
  untriaged is rejected. An item whose triage is in force is not listed again
  until its block changes."""
