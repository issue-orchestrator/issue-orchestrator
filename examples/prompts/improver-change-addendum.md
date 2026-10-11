## Invited: propose one change to the improver itself (#8001)

This run is invited to propose AT MOST ONE change to the improver: the
configuration that ran you, the champion <<CHAMPION>>. Propose one only if
your findings show the improver itself missing or mis-judging something a
different prompt passage, model, mode, heat count or budget would catch;
otherwise leave `improver_change` null.

A change is a challenger, not a decision. It is tried against the champion
on frozen snapshots of past engines, graded blind by cross-model graders
against answer keys written from hindsight (never by you), and replaces the
champion only if it scores better by more than the tournament's measured
noise AND a maintainer approves it.

Write it as the findings file's `improver_change`:

```json
"improver_change": {
  "edit": {"kind": "prompt", "find": "<a passage of THIS prompt, verbatim, that occurs exactly once (12-4000 characters)>", "replace": "<its replacement (up to 4000 characters)>"},
  "why": "<what the improver misses or mis-judges today, as your findings show it>",
  "expected_effect": "<what should score better if the change works>",
  "motivated_by": ["<the id of one of THIS file's findings or design findings>"]
}
```

`edit` is exactly one of:
- `{"kind": "prompt", "find": ..., "replace": ...}`: one exact passage of the
  prompt above (not this invitation, not the empowered addendum);
- `{"kind": "agent", "provider": "claude | codex", "model": "<model name>"}`;
- `{"kind": "mode", "mode": "scripted | empowered"}`;
- `{"kind": "heats", "heats": 1-5}`;
- `{"kind": "budget_minutes", "minutes": 10-240}`.

The edit must change the champion (not restate what it already is). Nothing
else can be changed: not the answer keys, the graders, the scoring or the
tournament.
