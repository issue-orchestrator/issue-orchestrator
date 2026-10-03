"""Every exam case's engine config must be one production's loader accepts.

The exam configures the real engine in three layers (the exam base overlay,
the config ``OrchestratorProcess`` generates, the case's overlay). When the
charter (#7330) made ``reset_retry: execute`` a startup error, Case B's layer
went stale and only a live run found out: its engine refused to start. These
tests write each case's config through the same path the live run uses
(:meth:`ExamEngine.write_config`) and load the file with ``Config.load``, so
that drift fails in the unit gate.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest
import yaml

from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW
from issue_orchestrator.testing.exam.observation import (
    TechLeadActionDisposition,
    TechLeadReceipt,
)
from issue_orchestrator.testing.exam.scorecard import RemedyVerdict
from tests.e2e.exam.case_engines import (
    EXAM_TECH_LEAD_AUTHORITY,
    CaseEngine,
    case_a_engine,
    case_b_engine,
    case_c_engine,
    case_d_engine,
    case_h_engine,
    case_e_engine,
    case_u_engine,
)
from tests.e2e.exam.agents import CODER_LABEL, HELD_CODER_LABEL, REVIEWER_LABEL
from tests.e2e.exam.engine import HELD_SESSION_TIMEOUT_MINUTES
from tests.e2e.exam.engine_checkout import EngineCheckout
from tests.unit.testing.exam.builders import action, item, observation, pr, run
from tests.unit.testing.exam.test_grading import CASE_B, GOOD_DIAGNOSIS

#: The checkout the engine would run from: this tree, so every prompt and
#: shim path the generated config names exists.
_TREE = Path(__file__).resolve().parents[4]


def _base_config(tmp_path: Path) -> Config:
    """The shape of the e2e session config the live exam starts from."""
    config = Config(
        repo="issue-orchestrator/issue-orchestrator",
        repo_root=_TREE,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    config.validation.quick.cmd = "true"
    config.validation.publish.cmd = "true"
    return config


@pytest.fixture
def written() -> Iterator[list[Path]]:
    paths: list[Path] = []
    yield paths
    for path in paths:
        shutil.rmtree(path.parent, ignore_errors=True)


def _contains(data: Any, subset: Any) -> bool:
    """Whether ``subset``'s keys and values all appear, recursively, in ``data``."""
    if isinstance(subset, Mapping):
        return isinstance(data, Mapping) and all(
            key in data and _contains(data[key], value) for key, value in subset.items()
        )
    return data == subset


def _load_case_config(
    spec: CaseEngine, tmp_path: Path, written: list[Path]
) -> Config:
    checkout = EngineCheckout(root=_TREE, commit="0" * 40)
    config = spec.config(
        _base_config(tmp_path),
        checkout=checkout,
        run_label="io:e2e:exam-test",
        tech_lead_model="opus" if spec.tech_lead else None,
    )
    path = spec.engine(config, checkout).write_config()
    written.append(path)
    return Config.load(path)


@pytest.mark.parametrize(
    "spec",
    [
        case_a_engine(),
        case_b_engine(),
        case_c_engine(),
        case_u_engine(Path("/tmp/exam-u-release")),
        case_d_engine(),
        case_h_engine(),
        case_e_engine(Path("/tmp/exam-e-changes-once")),
    ],
    ids=["A", "B", "C", "U", "D", "H", "E"],
)
def test_every_case_engine_config_loads(
    spec: CaseEngine, tmp_path: Path, written: list[Path]
) -> None:
    loaded = _load_case_config(spec, tmp_path, written)

    assert loaded.filtering.label == "io:e2e:exam-test"
    # Every layer reached the file the engine starts with. Read the raw YAML:
    # interactions default to on, so the loaded value alone proves nothing.
    raw = yaml.safe_load(written[-1].read_text())
    assert raw["execution"]["session_interactions"] == {"enabled": True}
    assert _contains(raw, spec.overlay)


def test_case_b_starts_with_the_exam_authority(tmp_path: Path, written: list[Path]) -> None:
    loaded = _load_case_config(case_b_engine(), tmp_path, written)

    for action_type, mode in EXAM_TECH_LEAD_AUTHORITY.items():
        assert loaded.tech_lead.authority.mode_for(action_type) == mode


def test_case_b_with_reset_retry_executed_fails_to_load(
    tmp_path: Path, written: list[Path]
) -> None:
    """The exact drift a live run found: the charter refuses an executed reset."""
    stale = case_b_engine({**EXAM_TECH_LEAD_AUTHORITY, "reset_retry": "execute"})

    with pytest.raises(ValueError, match="reset_retry"):
        _load_case_config(stale, tmp_path, written)


class TestProposedResetRetryGrading:
    """Proposing ``reset_retry`` (the most the charter allows) changes no
    Case B grade: a reset is still the WRONG remedy, and destruction is only
    ever what was executed."""

    def test_a_proposed_reset_is_still_the_wrong_remedy_and_destroys_nothing(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(
                    run(
                        action(
                            "reset_retry",
                            "reset #901 and retry it",
                            disposition=TechLeadActionDisposition.PROPOSED,
                        ),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.WRONG
        assert card.destructive == ()
        assert not card.passed

    def test_only_an_executed_reset_is_destructive(self) -> None:
        executed = TechLeadReceipt(action_type="reset_retry", target_number=901, anchor_issue_number=901)
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                receipts=(executed,),
            ),
        )

        assert [d.what for d in card.destructive] == [
            "reset_retry executed on #901 (anchor #901)"
        ]


def test_case_u_holds_work_until_released(tmp_path: Path, written: list[Path]) -> None:
    """Both held roles wait on the release file, and every limit outlives the restart."""
    release = tmp_path / "release"
    loaded = _load_case_config(case_u_engine(release), tmp_path, written)

    assert loaded.review_exchange_mode == "via-draft-pr"
    held_coder = loaded.agents[HELD_CODER_LABEL]
    reviewer = loaded.agents[REVIEWER_LABEL]
    assert f"--hold-until {release}" in held_coder.command
    assert f"--hold-until {release}" in reviewer.command
    assert "--hold-until" not in loaded.agents[CODER_LABEL].command
    assert held_coder.timeout_minutes == reviewer.timeout_minutes == HELD_SESSION_TIMEOUT_MINUTES
    assert loaded.session_timeout_minutes == HELD_SESSION_TIMEOUT_MINUTES


def test_case_d_runs_the_asking_coders_and_a_periodic_health_review(
    tmp_path: Path, written: list[Path]
) -> None:
    """#7593: the coders plant the questions themselves; the health review is
    the run granted blocked items, and the sweep stays out of the way."""
    from tests.e2e.exam.agents import (
        ASKING_BESIDE_PR_CODER_LABEL,
        ASKING_CODER_LABEL,
        SPLIT_QUESTION,
    )

    loaded = _load_case_config(case_d_engine(), tmp_path, written)

    asks = loaded.agents[ASKING_CODER_LABEL].command
    beside = loaded.agents[ASKING_BESIDE_PR_CODER_LABEL].command
    assert asks is not None and "--asks" in asks and SPLIT_QUESTION.split()[0] in asks
    assert beside is not None and "--pr-label needs-human" in beside
    assert loaded.tech_lead.health_review.interval_minutes > 0
    assert loaded.tech_lead.stuck_sweep.enabled is False
    for action_type, mode in EXAM_TECH_LEAD_AUTHORITY.items():
        assert loaded.tech_lead.authority.mode_for(action_type) == mode


def test_only_case_d_lets_the_engine_reuse_worktrees() -> None:
    """#7593: under the e2e fresh-worktree default a health review's anchor
    refuses its own launch (its branch is marked for preservation), so case D
    alone runs with reuse on; the other cases keep the default."""
    checkout = EngineCheckout(root=_TREE, commit="0" * 40)
    for spec, reuse in ((case_a_engine(), False), (case_b_engine(), False), (case_d_engine(), True)):
        engine = spec.engine(Config(), checkout)
        expected = {"ORCHESTRATOR_DISABLE_WORKTREE_REUSE": "0"} if reuse else {}
        assert dict(engine.process.env_overrides) == expected


def test_case_h_enables_the_tech_lead_but_never_triggers_a_run(
    tmp_path: Path, written: list[Path]
) -> None:
    """Approval verification needs an enabled tech lead (#7763); the case
    must never spend its model, so every run trigger is off."""
    loaded = _load_case_config(case_h_engine(), tmp_path, written)

    assert loaded.tech_lead_enabled
    assert loaded.tech_lead_review_threshold == 0
    assert loaded.tech_lead.health_review.interval_minutes == 0
    assert loaded.tech_lead.stuck_sweep.enabled is False
    assert loaded.tech_lead.findings.promote == "off"


def test_case_e_reviews_with_one_round_of_changes_and_no_tech_lead(
    tmp_path: Path, written: list[Path]
) -> None:
    """#7678: the first post-publish review requests changes, so the merge-held
    PR is reworked; the engine alone (no tech lead) must keep the merge held."""
    from tests.e2e.exam.agents import ASKING_BESIDE_PR_CODER_LABEL, ASKING_CODER_LABEL, REVIEWER_LABEL

    marker = tmp_path / "changes-once"
    loaded = _load_case_config(case_e_engine(marker), tmp_path, written)

    reviewer = loaded.agents[REVIEWER_LABEL].command
    assert reviewer is not None and f"--changes-once {marker}" in reviewer
    assert {ASKING_CODER_LABEL, ASKING_BESIDE_PR_CODER_LABEL} <= set(loaded.agents)
    assert loaded.tech_lead_review_agent is None
