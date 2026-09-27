"""The tech-lead prompt and the charter config say the same thing (#7330).

The agent reads ``tech-lead-charter.md`` (generated at launch); the orchestrator
enforces :class:`TechLeadCharterPolicy` at completion. These tests hold the two
together: for a grid of configs, every action type the agent can propose is
listed in the prompt under exactly the outcome the policy decides for it.
"""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.control.tech_lead_charter_prompt import (
    CHARTER_OUTCOME_LABELS,
    TECH_LEAD_CHARTER_FILENAME,
    render_tech_lead_charter_prompt,
    write_tech_lead_charter_prompt,
)
from issue_orchestrator.domain.tech_lead_artifacts import VALID_TECH_LEAD_ACTION_TYPES
from issue_orchestrator.domain.tech_lead_charter import CharterRole
from issue_orchestrator.execution.tech_lead_board_prompt import TECH_LEAD_CHARTER_SECTION
from issue_orchestrator.infra.config import Config

from tests.unit.test_tech_lead_prompt_contract import PROMPT_VARIANTS

REPO_ROOT = Path(__file__).resolve().parents[2]
_LABEL_TO_OUTCOME = {label: outcome for outcome, label in CHARTER_OUTCOME_LABELS.items()}
_ROLE_HEADING = re.compile(r"^## (\w+) \((.+)\)$")


def _parse(text: str) -> dict[str, tuple[str, str, object]]:
    """kind -> (role, dials, outcome), read back out of the rendered prompt."""
    placements: dict[str, tuple[str, str, object]] = {}
    role = dials = ""
    for line in text.splitlines():
        heading = _ROLE_HEADING.match(line)
        if heading:
            role, dials = heading.group(1), heading.group(2)
            continue
        if not line.startswith("- ") or ": " not in line:
            continue
        label, _, rest = line[2:].partition(": ")
        if label not in _LABEL_TO_OUTCOME:
            continue
        for kind in re.findall(r"`([a-z_]+)`", rest):
            assert kind not in placements, f"{kind} listed twice"
            placements[kind] = (role, dials, _LABEL_TO_OUTCOME[label])
    return placements


def _configs() -> list[Config]:
    configs = [Config()]
    for depth, authority, enabled in itertools.product(
        ("workaround", "fix", "restructure"), ("propose", "execute"), (True, False)
    ):
        for role in ("flow", "review_loop", "learning"):
            config = Config()
            dials = getattr(config.tech_lead.charter, role)
            dials.depth, dials.authority, dials.enabled = depth, authority, enabled
            config.tech_lead.authority.post_comment = "propose" if not enabled else "execute"
            configs.append(config)
    return configs


@pytest.mark.parametrize("config", _configs())
def test_prompt_lists_every_action_under_the_outcome_the_policy_enforces(
    config: Config,
) -> None:
    policy = TechLeadCharterPolicy.from_config(config)

    placements = _parse(render_tech_lead_charter_prompt(policy))

    # Every agent-proposable type appears exactly once; orchestrator-originated
    # kinds (finding promotion) are not the agent's to propose.
    assert set(placements) == set(VALID_TECH_LEAD_ACTION_TYPES)
    for kind, (role, dials, outcome) in placements.items():
        verdict = policy.decide(kind)  # computed independently of the render
        assert role == verdict.role.value, kind
        assert outcome is verdict.outcome, kind
        charter = policy.charter.for_role(verdict.role)
        expected_dials = (
            f"depth: {charter.depth.value}, authority: {charter.authority.value}"
            if charter.enabled
            else "disabled"
        )
        assert dials == expected_dials


def test_every_role_is_stated_even_when_nothing_maps_to_it() -> None:
    text = render_tech_lead_charter_prompt(TechLeadCharterPolicy.from_config(Config()))

    for role in CharterRole:
        assert re.search(rf"^## {role.value} \(", text, re.MULTILINE), role
    assert "- No action type you can propose maps to this role yet." in text


def test_prompt_tells_the_agent_it_cannot_choose_a_role() -> None:
    text = render_tech_lead_charter_prompt(TechLeadCharterPolicy.from_config(Config()))
    assert "You cannot choose a role or a depth" in text


def test_launch_writes_the_generated_charter(tmp_path: Path) -> None:
    config = Config()
    config.tech_lead.charter.flow.authority = "propose"
    policy = TechLeadCharterPolicy.from_config(config)

    path = write_tech_lead_charter_prompt(policy, tmp_path)

    assert path == tmp_path / "tech-lead-data" / TECH_LEAD_CHARTER_FILENAME
    assert path.read_text() == render_tech_lead_charter_prompt(policy)
    assert _parse(path.read_text())["create_issue"][2].value == "proposed"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_every_prompt_variant_points_the_agent_at_the_charter(variant: str) -> None:
    text = PROMPT_VARIANTS[variant]
    assert TECH_LEAD_CHARTER_SECTION in text, f"{variant} drifted from the charter section"
    assert f"tech-lead-data/{TECH_LEAD_CHARTER_FILENAME}" in TECH_LEAD_CHARTER_SECTION


# -- one enforcement owner ----------------------------------------------------

#: The only modules allowed to read the authority dials directly. Config models
#: define and validate them; the policy owner decides with them.
_DIAL_OWNERS = {
    "src/issue_orchestrator/control/tech_lead_charter_policy.py",
    "src/issue_orchestrator/infra/config_models_tech_lead.py",
    "src/issue_orchestrator/infra/config_models_tech_lead_charter.py",
    "src/issue_orchestrator/infra/config_sections_tech_lead.py",
    "src/issue_orchestrator/infra/settings_schema.py",
}


#: Validation, not decision: startup checks run each dial block's own
#: ``startup_errors``.
_DIAL_OWNERS_VALIDATION = {"src/issue_orchestrator/infra/validators/review.py"}

_DIAL_BLOCKS = {"tech_lead": {"authority", "charter"}, "findings": {"gated", "promote"}}


def _tail(node: ast.expr) -> str:
    """The last name in an attribute chain: ``config.tech_lead`` -> ``tech_lead``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _bindings(node: ast.AST) -> list[tuple[ast.expr | None, list[ast.expr]]]:
    """(value, targets) for every name-binding form that can carry an alias:
    plain, annotated and walrus assignment, and ``with ... as`` items."""
    if isinstance(node, ast.Assign):
        return [(node.value, list(node.targets))]
    if isinstance(node, ast.AnnAssign):
        return [(node.value, [node.target])]
    if isinstance(node, ast.NamedExpr):
        return [(node.value, [node.target])]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [
            (item.context_expr, [item.optional_vars])
            for item in node.items
            if item.optional_vars is not None
        ]
    return []


def _dial_reads(tree: ast.AST) -> list[str]:
    """Every way to REACH a dial block, not just a leaf read.

    Flagging ``<x>.tech_lead.authority`` itself (and ``<x>.findings.gated``)
    catches aliasing at its source — ``modes = config.tech_lead.authority``
    then ``modes.create_issue`` is reported at the assignment — as well as
    ``getattr(config.tech_lead, "authority")`` and ``.mode_for(...)``.
    """
    # Names bound to ``<x>.tech_lead`` (``lead = config.tech_lead``) reach the
    # dial blocks through a bare Name, so they are tracked as aliases.
    aliases = {
        target.id
        for node in ast.walk(tree)
        for value, targets in _bindings(node)
        if isinstance(value, ast.Attribute) and value.attr == "tech_lead"
        for target in targets
        if isinstance(target, ast.Name)
    }
    reads: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            receiver = _tail(node.value)
            if isinstance(node.value, ast.Name) and receiver in aliases:
                receiver = "tech_lead"
            if node.attr == "mode_for":
                reads.append("mode_for")
            elif node.attr in _DIAL_BLOCKS.get(receiver, ()) and (
                isinstance(node.value, ast.Attribute)
                or receiver == "findings"
                or (isinstance(node.value, ast.Name) and node.value.id in aliases)
            ):
                reads.append(f"{receiver}.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _DIAL_BLOCKS.get(_tail(node.args[0]), ())
        ):
            reads.append(f"getattr({_tail(node.args[0])}, {node.args[1].value!r})")
    return reads


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("modes = config.tech_lead.authority\nx = modes.create_issue", ["tech_lead.authority"]),
        ("c = self.config.tech_lead.charter.flow", ["tech_lead.charter"]),
        ("g = getattr(config.tech_lead, 'authority')", ["getattr(tech_lead, 'authority')"]),
        ("findings = config.tech_lead.findings\ng = findings.gated", ["findings.gated"]),
        ("m = cfg.authority.mode_for('x')", ["mode_for"]),
        # The tech-lead COMPOSITION's authority store is not a dial.
        ("store = tech_lead.authority", []),
        (
            "lead = config.tech_lead\nmodes = lead.authority\nx = modes.create_issue",
            ["tech_lead.authority"],
        ),
        (
            "lead: TechLeadConfig = config.tech_lead\nx = lead.authority.post_comment",
            ["tech_lead.authority"],
        ),
        ("if (lead := config.tech_lead):\n    x = lead.charter", ["tech_lead.charter"]),
    ],
)
def test_the_guard_sees_aliased_and_indirect_dial_reads(source: str, expected: list[str]) -> None:
    assert _dial_reads(ast.parse(source)) == expected


def test_authority_dials_are_read_only_by_the_policy_owner() -> None:
    """ONE owner decides authority: no module may consult a dial directly."""
    offenders: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "src" / "issue_orchestrator").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in _DIAL_OWNERS | _DIAL_OWNERS_VALIDATION:
            continue
        reads = _dial_reads(ast.parse(path.read_text(), filename=relative))
        if reads:
            offenders[relative] = reads
    assert offenders == {}
