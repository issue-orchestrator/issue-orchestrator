"""Server/client fingerprint parity for compact kanban cards.

The dashboard renders cards twice on initial open: once server-side
(Jinja → ``data-card-fingerprint`` attribute) and once client-side
(``compactCardState.computeCompactCardFingerprint`` after the first
``refreshViewModel`` returns). For the client to recognise the
server-rendered card as up-to-date and skip replacing the DOM node, the
two fingerprints MUST match byte-for-byte for the same card data.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.view_models.dashboard_flow import (
    compact_card,
    compute_compact_card_fingerprint,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
JS_HELPER = REPO_ROOT / "src" / "issue_orchestrator" / "static" / "js" / "compact_card_state.js"


def _js_fingerprint(card: dict[str, Any]) -> str:
    """Compute the JS fingerprint by spawning node — keeps the parity check
    honest even if either implementation drifts in isolation."""
    runner = textwrap.dedent(
        f"""
        const helper = require('{JS_HELPER}');
        const card = JSON.parse(process.argv[1]);
        process.stdout.write(helper.computeCompactCardFingerprint(card));
        """
    )
    result = subprocess.run(
        ["node", "-e", runner, json.dumps(card)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


REPRESENTATIVE_CARDS: list[dict[str, Any]] = [
    {
        "card_id": "issue-101",
        "issue_number": 101,
        "issue_key": None,
        "issue_label": "#101",
        "title": "Quiet boot",
        "state_label": "queued",
        "phase": "Queued",
        "phase_age": "5m",
        "summary": "",
        "is_stale": False,
        "stale_reason": "",
        "issue_url": "https://example.test/101",
        "pr_url": "",
        "github_url": "https://example.test/101",
        "github_label": "↗",
        "github_title": "Open issue on GitHub",
        "github_aria_label": "Open issue #101 on GitHub",
        "orchestrator_labels": ["agent:web", "blocked-needs-human"],
    },
    {
        "card_id": "issue-202",
        "issue_number": 202,
        "issue_key": "M3-007",
        "issue_label": "M3-007 · #202",
        "title": "Running task with stale flag",
        "state_label": "running",
        "phase": "Coding",
        "phase_age": "12s",
        "summary": "in flight",
        "is_stale": True,
        "stale_reason": "no event in 30m",
        "issue_url": "https://example.test/202",
        "pr_url": "https://example.test/pr/202",
        "github_url": "https://example.test/pr/202",
        "github_label": "PR ↗",
        "github_title": "Open PR on GitHub",
        "github_aria_label": "Open PR for issue #202 on GitHub",
        "orchestrator_labels": [],
    },
    # Edge: missing fields default to empty strings on both sides
    {
        "card_id": "issue-303",
        "issue_number": 303,
    },
]


@pytest.mark.parametrize("card", REPRESENTATIVE_CARDS, ids=lambda c: f"issue-{c['issue_number']}")
def test_python_and_js_fingerprints_match(card: dict[str, Any]) -> None:
    py = compute_compact_card_fingerprint(card)
    js = _js_fingerprint(card)
    assert py == js, (
        "Python and JS fingerprints diverged — initial dashboard refresh "
        "will replace every kanban card on open and re-introduce the flash.\n"
        f"  python: {py!r}\n  js:     {js!r}"
    )


def test_phase_age_is_excluded_from_fingerprint() -> None:
    base = dict(REPRESENTATIVE_CARDS[1])
    other = dict(base)
    other["phase_age"] = "1h"  # ticked
    assert compute_compact_card_fingerprint(base) == compute_compact_card_fingerprint(other)


def test_compact_card_attaches_fingerprint_field() -> None:
    card = compact_card({
        "issue_number": 999,
        "title": "Coverage",
        "status": "queued",
        "flow_stage_label": "Queued",
    })
    assert "fingerprint" in card
    assert card["fingerprint"] == compute_compact_card_fingerprint(card)


def test_phase_change_does_change_fingerprint() -> None:
    """Sanity check — only volatile fields should be excluded."""
    base = dict(REPRESENTATIVE_CARDS[1])
    other = dict(base)
    other["phase"] = "Reviewing"
    assert compute_compact_card_fingerprint(base) != compute_compact_card_fingerprint(other)
