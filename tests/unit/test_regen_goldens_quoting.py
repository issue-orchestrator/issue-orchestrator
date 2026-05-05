"""Quoting safety tests for `scripts/regen_goldens.py`.

The regen script emits most timeline rows as YAML *flow mappings*
(`{ key: value, key: value }`). In that context, any unquoted scalar
containing a flow indicator (`,`, `[`, `]`, `{`, `}`) breaks the
mapping — the flow indicator is parsed as syntax, not as part of the
scalar.

Timeline `summary` and `detail` strings are user-facing prose: they
already commonly contain commas (test name lists, "Branch X.Y, ..."),
brackets (CLI invocations), and braces (rare but possible). When the
projection produces such a string and regen emits it without quotes,
the regenerated fixture becomes invalid YAML — a bug the ordinary
golden tests cannot catch because they only assert on values that
already happen to be quote-safe.

These tests pin the quoting contract directly: load a fixture whose
records produce flow-indicator-containing values, run regen, then
re-parse and confirm the field round-trips intact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.regen_goldens import _needs_quoting, _yaml_scalar, regen_fixture  # noqa: E402


# ---------------------------------------------------------------------------
# Direct unit tests on the quoting predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "foo, bar",                    # comma → flow-mapping separator
        "[bracketed]",                 # brackets → flow-sequence syntax
        "{braced}",                    # braces  → flow-mapping syntax
        "make test-unit, lint-arch",   # commas in commands
        "stage: push, retryable",      # both colon and comma
        "items: [a, b, c]",            # nested-looking
        "{key: value}",                # mapping-looking text
        "first\nsecond",               # newline → line-fold corruption
        "carriage\rreturn",            # \r → invisible mangle in some readers
        "tabbed\there",                # tab → control whitespace
        "multi\nline\nprose",          # multiple newlines (agent prose)
    ],
)
def test_flow_indicators_force_quoting(value: str) -> None:
    """`_needs_quoting` must return True for any string containing a
    flow indicator or control character anywhere in its body."""
    assert _needs_quoting(value), f"flow-indicator value not quoted: {value!r}"


@pytest.mark.parametrize(
    "value",
    [
        "foo, bar",
        "[bracketed]",
        "{braced}",
        "make test-unit, lint-arch",
        # Control characters: a literal newline inside a double-quoted
        # scalar gets folded by PyYAML into a space, silently changing
        # the value. The renderer must escape it as backslash-n.
        "first\nsecond",
        "before\nafter\nend",
        "with\rcarriage",
        "with\ttab",
        # Combined: newline + flow indicator. A common shape for an
        # agent-written `implementation` summary.
        "Step 1: did x\nStep 2: did y, then z",
    ],
)
def test_yaml_scalar_renders_flow_safe(value: str) -> None:
    """The string emitted by `_yaml_scalar`, when wrapped in a
    flow-style mapping, must round-trip through PyYAML to the
    original value."""
    rendered = _yaml_scalar(value)
    flow_doc = f"{{ field: {rendered} }}"
    parsed = yaml.safe_load(flow_doc)
    assert parsed == {"field": value}, (
        f"flow-style scalar did not round-trip: input={value!r} "
        f"rendered={rendered!r} parsed={parsed!r}"
    )


# ---------------------------------------------------------------------------
# End-to-end: regen a fixture with a flow-indicator value; re-parse intact
# ---------------------------------------------------------------------------


_FIXTURE_TEMPLATE = """\
scenario: regen_quoting_probe
description: |
  Synthetic fixture. The `implementation` field on `session.completed`
  flows into the projected `detail` field — and contains a comma so
  regen must quote it inside flow-style mappings.
issue_number: 42
records:
  - event: session.started
    timestamp: "2026-01-01T00:00:00Z"
    data: {{ issue_number: 42, session_id: "issue-42-1", agent: coder, task: code }}
  - event: session.completed
    timestamp: "2026-01-01T00:01:00Z"
    data:
      issue_number: 42
      session_id: "issue-42-1"
      implementation: {implementation}
internal_timeline:
  []
"""


@pytest.mark.parametrize(
    "implementation,expected_detail",
    [
        ('"foo, bar"', "foo, bar"),
        ('"[wrapped]"', "[wrapped]"),
        ('"{stuff}"', "{stuff}"),
        ('"a, b, c"', "a, b, c"),
        # Newline-bearing values (agent prose: implementation/problem
        # summaries spanning multiple lines). Without explicit
        # backslash-n escaping, the regen output puts a literal LF
        # inside a flow-style double-quoted scalar, which PyYAML reads
        # back as a space-folded single line and silently corrupts the
        # detail.
        (
            '"first line\\nsecond line"',
            "first line\nsecond line",
        ),
        (
            '"step 1: did a, b\\nstep 2: did c"',
            "step 1: did a, b\nstep 2: did c",
        ),
    ],
)
def test_regen_emits_round_trippable_yaml_for_flow_indicator_values(
    tmp_path: Path,
    implementation: str,
    expected_detail: str,
) -> None:
    """Walk the full regen path: write a fixture whose projection
    produces a flow-indicator value in `detail`, regen, and confirm
    the regenerated YAML re-parses with the value intact.

    Without proper quoting, the regenerated `detail: foo, bar` (no
    quotes) inside `{ ..., detail: foo, bar, parent_key: ... }` would
    be parsed as a stray `bar` key with empty value, mangling the
    whole row.
    """
    fixture_path = tmp_path / "probe.yaml"
    fixture_path.write_text(_FIXTURE_TEMPLATE.format(implementation=implementation))

    # Smoke: input parses cleanly first.
    yaml.safe_load(fixture_path.read_text())

    regen_fixture(fixture_path)

    regenerated = yaml.safe_load(fixture_path.read_text())
    assert "internal_timeline" in regenerated
    rows = regenerated["internal_timeline"]
    completed_row = next(
        (r for r in rows if r.get("event") == "session.completed"),
        None,
    )
    assert completed_row is not None, (
        f"session.completed row missing from regenerated fixture: {rows!r}"
    )
    assert completed_row.get("detail") == expected_detail, (
        f"detail did not round-trip — quoting failed.\n"
        f"  expected: {expected_detail!r}\n"
        f"  got:      {completed_row.get('detail')!r}\n"
        f"  full row: {completed_row!r}"
    )
