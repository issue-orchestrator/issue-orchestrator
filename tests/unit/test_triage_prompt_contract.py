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


# --- Per-flavor flow isolation (#6763 finding 1) ---------------------------
#
# A health or failure-investigation session gets no PR manifest. If the
# manifest-read step or the "Empty batch" artifact-pair fallback lives in a
# section those flavors are told to follow, the session either fails on its
# intentionally absent manifest or publishes an empty-batch result instead of
# walking the board. Each prompt source must therefore isolate every
# batch-only instruction inside the Batch Review Flow. These strings are the
# unambiguous batch-only tells: the `manifest.json` read step and the literal
# "Empty batch" summary the empty-audit fallback writes.
_BATCH_ONLY_TELLS = ("manifest.json", "Empty batch")


def _flow_section(text: str, heading: str) -> str:
    """The named ``## <heading>`` flow, up to the next level-2 heading.

    Level-3 subsections (``### 1. Read the Manifest`` etc.) belong to their
    parent flow, so the section runs until the next ``## `` that is not
    ``### ``.
    """
    marker = f"## {heading}"
    assert marker in text, f"{heading!r} section missing"
    start = text.index(marker)
    rest = text[start + len(marker) :]
    match = re.search(r"\n## (?!#)", rest)
    end = start + len(marker) + (match.start() if match else len(rest))
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_three_flavor_flows_are_present(variant: str) -> None:
    """The per-flavor structure the guardrails rely on exists in every source."""
    text = PROMPT_VARIANTS[variant]
    for heading in (
        "Batch Review Flow",
        "Failure Investigation Flow",
        "Health Review Flow",
    ):
        assert f"## {heading}" in text, f"{variant} missing '## {heading}'"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_still_carries_the_manifest_and_empty_batch_steps(
    variant: str,
) -> None:
    """Non-vacuity: the tells the other flows must NOT contain live in batch."""
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")
    for tell in _BATCH_ONLY_TELLS:
        assert tell in batch, f"{variant} batch flow lost the {tell!r} step"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
@pytest.mark.parametrize(
    "heading", ["Health Review Flow", "Failure Investigation Flow"]
)
def test_non_batch_flows_contain_no_batch_only_instructions(
    variant: str, heading: str
) -> None:
    """A no-manifest flavor never inherits a manifest-read or empty-batch step."""
    section = _flow_section(PROMPT_VARIANTS[variant], heading)
    for tell in _BATCH_ONLY_TELLS:
        assert tell not in section, (
            f"{variant} '{heading}' contains batch-only instruction {tell!r}"
        )
