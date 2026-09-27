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
)
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
    [case_a_engine(), case_b_engine(), case_c_engine()],
    ids=["A", "B", "C"],
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
