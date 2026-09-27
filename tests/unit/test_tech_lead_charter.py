"""The tech-lead charter: classification, the decision grid, and config (#7330)."""

from __future__ import annotations

import itertools
from typing import get_args

import pytest

from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.tech_lead_artifacts import (
    VALID_TECH_LEAD_ACTION_TYPES,
    TechLeadActionType,
)
from issue_orchestrator.domain.tech_lead_charter import (
    CHARTER_ACTION_CLASSES,
    ORCHESTRATOR_CHARTER_ACTION_KINDS,
    PROMOTE_FINDING_KIND,
    CharterAuthority,
    CharterBinding,
    CharterDepth,
    CharterOutcome,
    CharterReason,
    CharterRole,
    RoleCharter,
    TechLeadCharter,
    UnclassifiedCharterActionError,
    classify_charter_action,
    decide_charter,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import TechLeadConfig
from issue_orchestrator.infra.config_models_tech_lead_charter import (
    TechLeadCharterConfig,
)
from issue_orchestrator.infra.config_sections_tech_lead import parse_tech_lead_config

ALL_KINDS = tuple(sorted(CHARTER_ACTION_CLASSES))
DEPTHS = tuple(CharterDepth)
AUTHORITIES = tuple(CharterAuthority)


def _charter(**roles: RoleCharter) -> TechLeadCharter:
    base = dict(TechLeadCharter.default().roles)
    base.update({CharterRole(name): charter for name, charter in roles.items()})
    return TechLeadCharter(roles=base)


# -- classification: one owner, total over every action kind ----------------


def test_every_action_kind_has_exactly_one_classification() -> None:
    """A new action type without a charter row fails HERE, not in production.

    The agent vocabulary is read from the ``TechLeadActionType`` Literal itself,
    so adding a type there without classifying it breaks this test.
    """
    agent_kinds = set(get_args(TechLeadActionType))
    assert agent_kinds == set(VALID_TECH_LEAD_ACTION_TYPES)
    assert set(CHARTER_ACTION_CLASSES) == agent_kinds | set(
        ORCHESTRATOR_CHARTER_ACTION_KINDS
    )


def test_an_unclassified_kind_raises_rather_than_being_guessed() -> None:
    with pytest.raises(UnclassifiedCharterActionError, match="no charter classification"):
        classify_charter_action("close_pull_request")
    with pytest.raises(UnclassifiedCharterActionError):
        decide_charter(
            "close_pull_request",
            TechLeadCharter.default(),
            action_ceiling=CharterAuthority.EXECUTE,
            ceiling_source="x",
        )


def test_every_charter_role_has_a_config_field_and_nothing_else_does() -> None:
    import dataclasses

    names = tuple(item.name for item in dataclasses.fields(TechLeadCharterConfig))
    assert names == tuple(role.value for role in CharterRole)


def test_reset_retry_is_the_destructive_kind() -> None:
    destructive = {
        kind
        for kind, action_class in CHARTER_ACTION_CLASSES.items()
        if action_class.binding is CharterBinding.DESTRUCTIVE
    }
    assert destructive == {"reset_retry"}


def test_release_withheld_review_is_a_flow_fix_that_is_not_destructive() -> None:
    """#7399: removing the issue's own blocked-failed loses nothing, so it is
    approvable (restricted by the flow role's dials), never destructive."""
    action_class = classify_charter_action("release_withheld_review")

    assert action_class.role is CharterRole.FLOW
    assert action_class.depth is CharterDepth.FIX
    assert action_class.binding is CharterBinding.APPROVABLE


def test_release_withheld_review_default_authority_follows_the_charter() -> None:
    """Its per-action ceiling defaults open, so the flow role's dials decide:
    the default charter executes it, a proposing flow role proposes it, and a
    workaround-deep flow role keeps it as advice."""
    config = Config()
    assert config.tech_lead.authority.release_withheld_review == "execute"
    assert TechLeadCharterPolicy.from_config(config).decide("release_withheld_review").executes

    def verdict(flow: RoleCharter) -> CharterOutcome:
        return decide_charter(
            "release_withheld_review", _charter(flow=flow),
            action_ceiling=CharterAuthority.EXECUTE, ceiling_source="x",
        ).outcome

    assert verdict(RoleCharter(CharterDepth.FIX, CharterAuthority.PROPOSE)) is CharterOutcome.PROPOSED
    assert verdict(RoleCharter(CharterDepth.WORKAROUND, CharterAuthority.EXECUTE)) is CharterOutcome.ADVICE_ONLY


# -- the decision grid -------------------------------------------------------


@pytest.mark.parametrize(
    ("role_depth", "role_authority", "ceiling"),
    list(itertools.product(DEPTHS, AUTHORITIES, AUTHORITIES)),
)
def test_approvable_action_across_depth_by_authority_grid(
    role_depth: CharterDepth, role_authority: CharterAuthority, ceiling: CharterAuthority
) -> None:
    """create_issue needs flow at depth fix: below it is advice; within it,
    either authority dial saying propose sends it to the approval gate."""
    charter = _charter(flow=RoleCharter(depth=role_depth, authority=role_authority))

    verdict = decide_charter(
        "create_issue", charter, action_ceiling=ceiling, ceiling_source="c"
    )

    assert verdict.role is CharterRole.FLOW
    assert verdict.required_depth is CharterDepth.FIX
    if role_depth is CharterDepth.WORKAROUND:
        assert verdict.outcome is CharterOutcome.ADVICE_ONLY
        assert verdict.reason_code is CharterReason.BEYOND_DEPTH
        assert "deeper than flow's allowed depth (workaround)" in verdict.reason
    elif role_authority is CharterAuthority.PROPOSE:
        assert verdict.outcome is CharterOutcome.PROPOSED
        assert verdict.reason_code is CharterReason.ROLE_AUTHORITY_PROPOSE
        assert verdict.reason == (
            "Waiting on you: flow may fix but must propose (authority: propose)."
        )
    elif ceiling is CharterAuthority.PROPOSE:
        assert verdict.outcome is CharterOutcome.PROPOSED
        assert verdict.reason_code is CharterReason.ACTION_AUTHORITY_PROPOSE
    else:
        assert verdict.outcome is CharterOutcome.EXECUTED
        assert verdict.reason_code is CharterReason.WITHIN_CHARTER_EXECUTE


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_a_disabled_role_makes_every_restricted_action_advice(kind: str) -> None:
    action_class = classify_charter_action(kind)
    charter = _charter(
        **{
            action_class.role.value: RoleCharter(
                depth=CharterDepth.RESTRUCTURE,
                authority=CharterAuthority.EXECUTE,
                enabled=False,
            )
        }
    )

    verdict = decide_charter(
        kind, charter, action_ceiling=CharterAuthority.EXECUTE, ceiling_source="c"
    )

    if action_class.binding is CharterBinding.FLOOR:
        assert verdict.outcome is CharterOutcome.EXECUTED
    elif action_class.binding is CharterBinding.ADVISORY:
        # Noticing and advising are never restricted by the charter (#7329).
        assert verdict.outcome is CharterOutcome.EXECUTED
        assert verdict.reason_code is CharterReason.ADVISORY_EXECUTES
    else:
        assert verdict.outcome is CharterOutcome.ADVICE_ONLY
        assert verdict.reason_code is CharterReason.ROLE_DISABLED


def _every_charter() -> list[TechLeadCharter]:
    dials = [
        RoleCharter(depth=depth, authority=authority, enabled=enabled)
        for depth, authority, enabled in itertools.product(
            DEPTHS, AUTHORITIES, (True, False)
        )
    ]
    return [_charter(flow=dial) for dial in dials]


@pytest.mark.parametrize("ceiling", AUTHORITIES)
def test_destructive_actions_never_execute_whatever_the_charter_says(
    ceiling: CharterAuthority,
) -> None:
    for charter in _every_charter():
        verdict = decide_charter(
            "reset_retry", charter, action_ceiling=ceiling, ceiling_source="c"
        )
        assert verdict.outcome is not CharterOutcome.EXECUTED
        if charter.for_role(CharterRole.FLOW).enabled:
            assert verdict.outcome is CharterOutcome.REFUSED_DESTRUCTIVE
            assert verdict.awaits_approval
            assert verdict.reason.startswith("Always needs approval")


@pytest.mark.parametrize("kind", ["escalate_to_human", "defer_to_tracker"])
def test_floors_always_execute(kind: str) -> None:
    for charter in _every_charter():
        verdict = decide_charter(
            kind, charter, action_ceiling=CharterAuthority.PROPOSE, ceiling_source="c"
        )
        assert verdict.outcome is CharterOutcome.EXECUTED
        assert verdict.reason_code is CharterReason.FLOOR_ALWAYS_EXECUTES


@pytest.mark.parametrize("kind", ["post_comment", "flag_pattern"])
def test_advice_is_never_restricted_by_role_or_depth(kind: str) -> None:
    """Only the per-action mode decides an advisory kind (#7329 comment, 1)."""
    for charter, ceiling in itertools.product(_every_charter(), AUTHORITIES):
        charter = _charter(
            learning=charter.for_role(CharterRole.FLOW), flow=charter.for_role(CharterRole.FLOW)
        )
        verdict = decide_charter(kind, charter, action_ceiling=ceiling, ceiling_source="c")
        expected = (
            CharterOutcome.EXECUTED
            if ceiling is CharterAuthority.EXECUTE
            else CharterOutcome.ADVICE_ONLY
        )
        assert verdict.outcome is expected


# -- defaults reproduce the pre-charter behaviour exactly ---------------------


def _legacy_executes(config: Config, kind: str) -> bool:
    """What decided execution BEFORE the charter: the per-action mode alone."""
    if kind == PROMOTE_FINDING_KIND:
        return config.tech_lead.findings.promote == "auto"
    return config.tech_lead.authority.mode_for(kind) == "execute"


@pytest.mark.parametrize("promote", ["gated", "auto"])
@pytest.mark.parametrize(
    "modes",
    [
        {},
        {"post_comment": "propose", "create_issue": "propose", "flag_pattern": "propose"},
        {
            "kill_hung_session": "execute",
            "request_rework": "execute",
            "recover_validated_work": "execute",
        },
    ],
)
def test_default_charter_decides_exactly_what_the_modes_decided(
    modes: dict[str, str], promote: str
) -> None:
    config = Config()
    for key, value in modes.items():
        setattr(config.tech_lead.authority, key, value)
    config.tech_lead.findings.promote = promote
    policy = TechLeadCharterPolicy.from_config(config)

    for kind in ALL_KINDS:
        verdict = policy.decide(kind)
        assert verdict.executes == _legacy_executes(config, kind), kind
        # Nothing the default charter decides is advice that used to be a gate.
        if not verdict.executes and CHARTER_ACTION_CLASSES[kind].binding in (
            CharterBinding.APPROVABLE,
            CharterBinding.DESTRUCTIVE,
        ):
            assert verdict.awaits_approval, kind


def test_general_role_defaults_narrow_and_named_roles_default_wide() -> None:
    charter = TechLeadCharter.default()
    assert charter.for_role(CharterRole.GENERAL) == RoleCharter(
        depth=CharterDepth.WORKAROUND, authority=CharterAuthority.PROPOSE
    )
    for role in CharterRole:
        if role is not CharterRole.GENERAL:
            assert charter.for_role(role) == RoleCharter(
                depth=CharterDepth.RESTRUCTURE, authority=CharterAuthority.EXECUTE
            )


def test_promotion_is_decided_by_promote_mode_and_the_learning_role() -> None:
    config = Config()
    config.tech_lead.findings.promote = "auto"
    config.tech_lead.charter.learning.authority = "propose"
    policy = TechLeadCharterPolicy.from_config(config)

    assert policy.promotion().outcome is CharterOutcome.PROPOSED
    assert policy.promotion_gated() is True

    config.tech_lead.charter.learning.depth = "workaround"
    policy = TechLeadCharterPolicy.from_config(config)
    assert policy.promotion().advice_only

    config.tech_lead.findings.promote = "off"
    policy = TechLeadCharterPolicy.from_config(config)
    assert policy.promotion_gated() is False
    with pytest.raises(ValueError, match="promotion is off"):
        policy.promotion()


# -- config: fail-fast by shape -----------------------------------------------


def test_charter_omitted_or_null_is_every_default() -> None:
    assert parse_tech_lead_config({}).charter == TechLeadCharterConfig()
    assert parse_tech_lead_config({"charter": None}).charter == TechLeadCharterConfig()
    assert TechLeadConfig().charter.to_charter() == TechLeadCharter.default()


def test_charter_parses_dials_and_keeps_defaults_for_omitted_roles() -> None:
    charter = parse_tech_lead_config(
        {
            "charter": {
                "flow": {"depth": "fix", "authority": "propose"},
                "intake": {"enabled": False},
            }
        }
    ).charter.to_charter()

    assert charter.for_role(CharterRole.FLOW) == RoleCharter(
        depth=CharterDepth.FIX, authority=CharterAuthority.PROPOSE
    )
    intake = charter.for_role(CharterRole.INTAKE)
    assert intake.enabled is False
    # An omitted role is NOT disabled: a one-dial settings-form edit must never
    # silently switch off every other role.
    assert charter.for_role(CharterRole.PLATFORM) == TechLeadCharter.default().for_role(
        CharterRole.PLATFORM
    )


@pytest.mark.parametrize(
    ("charter", "message"),
    [
        ({"janitor": {"depth": "fix"}}, "unknown role"),
        ({"flow": {"depth": "rewrite"}}, "tech_lead.charter.flow.depth"),
        ({"flow": {"authority": "auto"}}, "tech_lead.charter.flow.authority"),
        ({"flow": {"enabled": "false"}}, "tech_lead.charter.flow.enabled"),
        ({"flow": {"role": "general"}}, "unknown key"),
        ({"flow": "execute"}, "must be a mapping"),
        (["flow"], "must be a mapping"),
    ],
)
def test_charter_rejects_invalid_shapes(charter: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_tech_lead_config({"charter": charter})


def test_settings_form_values_are_revalidated_at_startup() -> None:
    config = Config()
    config.tech_lead.charter.review_loop.depth = "deep"

    errors = config.validate()

    assert any("tech_lead.charter.review_loop.depth" in error for error in errors)


def test_charter_is_in_the_config_event_payload() -> None:
    payload = TechLeadConfig().to_event_dict()
    assert payload["charter"]["general"] == {
        "enabled": True,
        "depth": "workaround",
        "authority": "propose",
    }


# -- settings form: the schema closes the same vocabulary ---------------------


def test_settings_schema_projects_every_charter_dial() -> None:
    from issue_orchestrator.infra.settings_schema import ReviewSettings

    fields = ReviewSettings.model_fields
    for role in CharterRole:
        for dial in ("enabled", "depth", "authority"):
            name = f"tech_lead_charter_{role.value}_{dial}"
            extra = fields[name].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["yaml_path"] == f"tech_lead.charter.{role.value}.{dial}"
            assert extra["config_attr"] == f"tech_lead.charter.{role.value}.{dial}"
    assert fields["tech_lead_charter_general_authority"].default == "propose"
    assert fields["tech_lead_charter_flow_depth"].default == "restructure"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tech_lead_charter_flow_depth", "deep"),
        ("tech_lead_charter_general_authority", "auto"),
        ("tech_lead_authority_reset_retry", "execute"),
    ],
)
def test_settings_schema_rejects_values_the_charter_cannot_honour(
    field: str, value: str
) -> None:
    from pydantic import ValidationError

    from issue_orchestrator.infra.settings_schema import ReviewSettings

    with pytest.raises(ValidationError):
        ReviewSettings(**{field: value})


def test_a_programmatic_non_boolean_enabled_never_enables_a_role() -> None:
    """A truthy string must not switch a role on (review r3 F2)."""
    config = Config()
    setattr(config.tech_lead.charter.flow, "enabled", "false")

    errors = config.validate()

    assert any("tech_lead.charter.flow.enabled must be a boolean" in e for e in errors)
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        TechLeadCharterPolicy.from_config(config)
