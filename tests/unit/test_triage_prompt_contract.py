"""Guardrails keeping the three triage prompt variants on the artifact contract.

#6761 finding 10: the no-manifest ("no PRs") path used to instruct a bare
``coding-done`` completion, which guarantees a ``missing_decision`` rejection
under the mandatory-pair rule. Every prompt variant must instead show the
minimal valid empty-audit pair written BEFORE ``coding-done``, and the JSON it
shows must actually validate against the domain contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.triage_issue_policy import (
    protected_triage_label_violations,
)
from issue_orchestrator.domain.triage_artifacts import TriageDecision
from issue_orchestrator.entrypoints.setup_wizard_prompts import (
    build_triage_review_prompt_text,
)
from issue_orchestrator.infra.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]

PROMPT_VARIANTS = {
    "setup_wizard": build_triage_review_prompt_text("triage-review", "triage-reviewed"),
    "examples": (REPO_ROOT / "examples" / "prompts" / "triage-review.md").read_text(),
    "repo_specific": (REPO_ROOT / "repo-specific" / "prompts" / "triage.md").read_text(),
}

NO_MANIFEST_MARKER = "**If the manifest is missing or lists no PRs:**"


def _no_manifest_block(text: str) -> str:
    """The no-manifest instructions up to the next section heading."""
    assert NO_MANIFEST_MARKER in text, "no-manifest path missing from prompt"
    start = text.index(NO_MANIFEST_MARKER)
    match = re.search(r"\n#{2,3} ", text[start:])
    end = start + match.start() if match else len(text)
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_path_writes_pair_before_coding_done(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])

    assert "triage-decision.json" in block
    assert "triage-report.md" in block
    assert "before completing" in block
    # The pair-writing instructions come BEFORE the completion command.
    assert "coding-done" in block
    assert block.index("triage-decision.json") < block.index("coding-done completed")


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_empty_audit_json_is_contract_valid(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", block, re.DOTALL)
    assert match, "empty-audit heredoc JSON missing from no-manifest path"

    decision = TriageDecision.from_agent_payload(json.loads(match.group(1)))

    assert decision.findings == ()
    assert decision.proposed_actions == ()
    assert decision.summary


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_compact_decision_example_is_contract_valid(variant: str) -> None:
    """The worked example must parse (canonical ids, evidence, allowed labels)."""
    text = PROMPT_VARIANTS[variant]
    blocks = [
        block
        for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        if "proposed_actions" in block
    ]
    assert blocks, "compact triage-decision.json example missing"

    decision = TriageDecision.from_agent_payload(json.loads(blocks[0]))

    assert decision.findings, "example should demonstrate a finding"
    assert all(finding.evidence for finding in decision.findings)
    config = Config()
    labels = LabelManager(config)
    for action in decision.proposed_actions:
        assert (
            protected_triage_label_violations(
                action.labels, config=config, labels=labels
            )
            == []
        ), f"{variant} example proposes protected labels"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_investigation_flavor_documents_focus_comment_rule(variant: str) -> None:
    """Finding 2's rule is orchestrator-enforced; the prompts must teach it."""
    text = PROMPT_VARIANTS[variant]
    assert "focus_issue_number" in text
    marker = text.index('"flavor": "failure_investigation"')
    bullet = text[marker : marker + 800]
    assert "post_comment" in bullet
    assert "target_number" in bullet
