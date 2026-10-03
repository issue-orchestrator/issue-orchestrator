"""The typed rules of a ``resolve_block`` decision (#7658)."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.block_resolution import (
    RESOLVABLE_CAUSES,
    BlockResolution,
    HumanOnlyWork,
    cause_marker,
    human_only_work,
    prior_resolutions,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause


def _data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": "answer",
        "causes": ["agent_completion"],
        "title": "Rule it unholdable",
        "body": "ADR-0010 already rules it.",
        "evidence": ["docs/adr/0010.md"],
    }
    data.update(overrides)
    return data


def test_a_merge_decision_is_never_resolvable() -> None:
    from issue_orchestrator.domain.block_resolution import is_resolvable_work_block

    assert NeedsHumanCause.MERGE_DECISION.scope is not NeedsHumanCause.AGENT_COMPLETION.scope
    assert not is_resolvable_work_block(NeedsHumanCause.MERGE_DECISION)


def test_only_work_blocks_are_resolvable() -> None:
    assert RESOLVABLE_CAUSES == {NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE}
    for cause in set(NeedsHumanCause) - RESOLVABLE_CAUSES:
        with pytest.raises(ValueError, match="not resolvable"):
            BlockResolution.from_mapping(_data(kind="lift", causes=[cause.value]), context="A1")


def test_an_answer_or_split_answers_the_agents_question() -> None:
    with pytest.raises(ValueError, match="agent_completion"):
        BlockResolution.from_mapping(_data(causes=["session_lifecycle"]), context="A1")
    lift = BlockResolution.from_mapping(_data(kind="lift", causes=["session_lifecycle"]), context="A1")
    assert lift.causes == {NeedsHumanCause.SESSION_LIFECYCLE}


def test_evidence_is_required() -> None:
    with pytest.raises(ValueError, match="evidence"):
        BlockResolution.from_mapping(_data(evidence=[]), context="A1")


@pytest.mark.parametrize(
    ("children", "parent", "match"),
    [
        ([], "narrow", "1-3 children"),
        ([{"title": "a", "body": "b"}], None, "narrowed or closed"),
        ([{"title": "a", "body": "b", "edge": "depends_on", "after": 1}], "narrow", "EARLIER"),
        ([{"title": "a", "body": "b", "edge": "depends_on", "after": "parent"}], "close", "closes"),
        ([{"title": "a", "body": "b", "edge": "depends_on"}], "narrow", "or neither"),
        ([{"title": "a", "body": "Rest.\nDepends-on: #999999", "edge": "depends_on", "after": "parent"}],
         "narrow", "dependency line"),
    ],
)
def test_a_split_is_a_well_formed_graph(children, parent, match) -> None:
    with pytest.raises(ValueError, match=match):
        BlockResolution.from_mapping(_data(kind="split", children=children, parent=parent), context="A1")


def test_only_a_split_files_children() -> None:
    with pytest.raises(ValueError, match="only a split"):
        BlockResolution.from_mapping(
            _data(children=[{"title": "a", "body": "b"}], parent="narrow"), context="A1"
        )


def test_round_trip() -> None:
    data = _data(
        kind="split", causes=["agent_completion", "session_lifecycle"],
        children=[
            {"title": "a", "body": "b", "edge": None, "after": None},
            {"title": "c", "body": "d", "edge": "stack_after", "after": 1},
        ],
        parent="narrow",
    )
    resolution = BlockResolution.from_mapping(data, context="A1")
    assert BlockResolution.from_mapping(resolution.to_dict(), context="stored") == resolution


# -- the human-only screen, calibrated on porchpin's own text -----------------

#: porchpin#179's status note: maintainer account work, every line of it.
PORCHPIN_179 = (
    "What remains is maintainer account work, then one run of the workflow.\n"
    "> **Provisioning checklist (human; each is an account action)**\n"
    "> - [ ] Service token for the runner; least-privilege Cloudflare API deploy token"
)
#: Lines from the items a resolve must still be able to decide.
PORCHPIN_262 = (
    "GET /new/:pickupId renders the paste message from the Pickup DO's own view; no"
    " provider credential is set on this Worker. The seller-key match decides it."
)
PORCHPIN_326 = (
    "[CUJ:S0,R2,H1,H2,H3,H4] Seller account deletion: cancel-first, queued purge,"
    " legal-hold deferral, and BDR-0009"
)


def test_porchpin_179_is_human_only_work() -> None:
    match = human_only_work([PORCHPIN_179])
    assert match is not None
    assert match.category in {HumanOnlyWork.EXTERNAL_ACCOUNT, HumanOnlyWork.CREDENTIALS,
                              HumanOnlyWork.INFRASTRUCTURE_PROVISIONING}


@pytest.mark.parametrize("text", [PORCHPIN_262, PORCHPIN_326])
def test_code_that_mentions_accounts_or_credentials_is_not(text: str) -> None:
    assert human_only_work([text]) is None


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Please create an account on Fly.io for the staging app", HumanOnlyWork.EXTERNAL_ACCOUNT),
        ("Add the STRIPE secret to the repo's CI environment", HumanOnlyWork.CREDENTIALS),
        ("It needs a paid plan before the feature can be tested", HumanOnlyWork.MONEY),
        ("This wording needs legal review before launch", HumanOnlyWork.LEGAL),
        ("Point the DNS record at the new Worker", HumanOnlyWork.INFRASTRUCTURE_PROVISIONING),
    ],
)
def test_each_category(text: str, category: HumanOnlyWork) -> None:
    match = human_only_work([None, text])
    assert match is not None and match.category is category


def test_markers_record_each_discharged_cause_by_decision() -> None:
    comments = [
        "unrelated",
        f"decided\n{cause_marker(NeedsHumanCause.AGENT_COMPLETION, 'run-1/A1')}",
        cause_marker(NeedsHumanCause.SESSION_LIFECYCLE, "run-1/A1")
        + cause_marker(NeedsHumanCause.AGENT_COMPLETION, "run-2/A4"),
    ]
    assert prior_resolutions(comments) == {
        NeedsHumanCause.AGENT_COMPLETION: frozenset({"run-1/A1", "run-2/A4"}),
        NeedsHumanCause.SESSION_LIFECYCLE: frozenset({"run-1/A1"}),
    }


def test_a_child_title_github_would_refuse_rejects_the_decision() -> None:
    """r6 F1: GitHub caps an issue title at 256 characters; a longer child
    title would fail mid-split, after earlier children were filed."""
    children = [{"title": "First half", "body": "One."}, {"title": "x" * 257, "body": "Two."}]
    with pytest.raises(ValueError, match="256"):
        BlockResolution.from_mapping(_data(kind="split", children=children, parent="narrow"), context="A1")
    children[1]["title"] = "x" * 256
    BlockResolution.from_mapping(_data(kind="split", children=children, parent="narrow"), context="A1")
