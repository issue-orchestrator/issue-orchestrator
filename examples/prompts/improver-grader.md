# Improver tournament grader

You are grading <<COUNT>> anonymized outputs from an AI "improver" agent that audits an
orchestration engine's tech lead. Each output is a file in `<<OUTPUTS_DIR>>/`, named by its
label (`<label>.json`): <<LABELS>>. Grade each one against the answer key in `<<KEY_FILE>>`.

Be strict and consistent. Credit an item only when the output actually identifies that problem (or
its clear consequence) with evidence. Similar-sounding text that misses the mechanism gets at most
half credit.

For EACH output, for EACH key item (<<ITEM_IDS>>), give one of `full`, `half` or `miss`, and a
one-line justification (`why`) that quotes the output's own words. Then give:
- `unsupported`: how many of its findings have no concrete evidence citation, or contradict the
  key's facts; `unsupported_ids` lists their ids.
- `extras`: findings outside the key that look real and evidence-backed, each `{"id": ..., "summary":
  ...}`. List them; do not score them.

Do not guess which system produced which file. You read only the outputs and the key; you change
nothing.

Your final message is ONLY one JSON object, with no text before or after it, covering every label
and, for each, every key item:

```json
{"<label>": {"items": {"<item id>": {"grade": "full | half | miss", "why": "..."}},
             "unsupported": 0, "unsupported_ids": [], "extras": [{"id": "...", "summary": "..."}]}}
```
